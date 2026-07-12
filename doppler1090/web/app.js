// doppler1090 dashboard.
//
// The map is a fullscreen "scope"; a bottom console carries the time-travel
// scrubber and (when an aircraft is selected) the measured-vs-predicted plot.
// Live capture never stops server-side — scrubbing into the past is purely a
// read: the client either polls /api/state (live) or /api/state?at=T (past),
// while /api/timeline keeps the activity strip growing either way.

// Zoom locked to the range the FAA sectional cache actually covers (native
// tiles exist z8-z12; outside that the service 404s). Max 11 because
// detectRetina pulls one level deeper, so map-zoom 11 already shows z12 tiles.
const map = L.map('map', { minZoom: 7, maxZoom: 11, zoomControl: false })
  .setView([41.88, -87.63], 10);   // Chicago placeholder; recenters on receiver
// Zoom control on the right, by the layer picker — clear of the health strip.
L.control.zoom({ position: 'topright' }).addTo(map);
const osm = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  { maxZoom: 19, detectRetina: true, attribution: '© OpenStreetMap' });
const faaChart = name => L.tileLayer(
  `https://tiles.arcgis.com/tiles/ssFJjBXIUyZDrSYZ/arcgis/rest/services/${name}/MapServer/tile/{z}/{y}/{x}`,
  { maxZoom: 12, minNativeZoom: 8, maxNativeZoom: 12, opacity: 0.4,
    detectRetina: true, attribution: 'Aeronautical charts: FAA' });
// IFR enroute as one auto-switching style: high-altitude (decluttered) when
// zoomed out, low-altitude (detailed) when zoomed in. Both cover the whole
// country, so the swap never blanks the map.
const IFR_SWITCH_ZOOM = 9;
function ifrAuto() {
  const low = faaChart('IFR_AreaLow'), high = faaChart('IFR_High');
  const group = L.layerGroup();
  let cur = null;
  const pick = () => {
    const want = map.getZoom() >= IFR_SWITCH_ZOOM ? low : high;
    if (want === cur) return;
    if (cur) group.removeLayer(cur);
    group.addLayer(want); cur = want;
  };
  group.on('add', () => { pick(); map.on('zoomend', pick); });
  group.on('remove', () => { map.off('zoomend', pick);
    if (cur) { group.removeLayer(cur); cur = null; } });
  return group;
}

const baseLayers = {
  'VFR Sectional': faaChart('VFR_Sectional'),
  'IFR': ifrAuto(),
  'OpenStreetMap': osm,
};
baseLayers['VFR Sectional'].addTo(map);
L.control.layers(baseLayers, {}, { position: 'topright' }).addTo(map);

// ---- state ---------------------------------------------------------------
let receiverMarker = null, ringsDrawn = false;
let layers = {};              // icao -> { marker, tracks:[polyline], data, ghostSince }
let selLayers = [];           // line-of-sight + closest-approach overlays
let selected = null;
let latest = { aircraft: [] };
let serverNow = 0;            // most recent server_time seen

let mode = 'live';            // 'live' | 'past'
let viewTime = 0;             // epoch of the current past frame
let playing = false, speed = 1;
const SPEEDS = [1, 2, 5, 10, 20];   // playback multipliers cycled by the button
let tl = null;                // { start, end, buckets, recording }
let inflight = false;         // guards overlapping /api/state fetches

const GHOST_TTL_MS = 5 * 60 * 1000;
function ghostFade(ageMs) { return 0.5 - 0.38 * Math.min(1, ageMs / GHOST_TTL_MS); }

// Doppler (Hz) -> diverging color. + = approaching = blue, - = receding = red.
function dopColor(hz) {
  const x = Math.max(-1, Math.min(1, hz / 600));
  if (x >= 0) { const t = x;
    return `rgb(${Math.round(255*(1-t))},${Math.round(255*(1-t))},255)`; }
  const t = -x;
  return `rgb(255,${Math.round(255*(1-t))},${Math.round(255*(1-t))})`;
}

function findAircraft(icao) {
  const live = latest.aircraft.find(x => x.icao === icao);
  if (live) return { a: live, ghostSince: null };
  const e = layers[icao];
  if (e && e.data && e.ghostSince != null) return { a: e.data, ghostSince: e.ghostSince };
  return null;
}

// ---- map render ----------------------------------------------------------
function render(state) {
  latest = state;
  if (state.server_time) serverNow = state.server_time;
  if (!receiverMarker && state.receiver && state.receiver.lat != null) {
    receiverMarker = L.circleMarker([state.receiver.lat, state.receiver.lon],
      { radius: 7, color: '#000', weight: 2, fillColor: '#ffd400', fillOpacity: 1 })
      .addTo(map).bindTooltip('receiver');
    map.setView([state.receiver.lat, state.receiver.lon], map.getZoom());
    drawRangeRings(state.receiver);
  }
  const live = mode === 'live';
  const now = Date.now();
  const seen = new Set();
  for (const a of state.aircraft) {
    seen.add(a.icao);
    let entry = layers[a.icao];
    if (!entry) { entry = { marker: null, tracks: [] }; layers[a.icao] = entry; }
    entry.data = a;
    entry.ghostSince = null;
    entry.tracks.forEach(l => map.removeLayer(l));
    entry.tracks = [];
    const isSel = a.icao === selected;
    const g = a.ground_track;
    for (let i = 1; i < g.length; i++) {
      const seg = L.polyline([[g[i-1][0], g[i-1][1]], [g[i][0], g[i][1]]],
        { color: dopColor(g[i][2]), weight: isSel ? 7 : 4, opacity: 0.9 });
      seg.addTo(map); entry.tracks.push(seg);
    }
    if (a.lat != null) {
      const label = a.flight || a.icao;
      if (!entry.marker) {
        entry.marker = L.marker([a.lat, a.lon], { icon: planeIcon(a.track, isSel, false) })
          .addTo(map).on('click', () => select(a.icao))
          .bindTooltip(label, { permanent: false, direction: 'top',
                                offset: [0, -16], opacity: 0.95 });
        entry.label = label;
      } else {
        entry.marker.setLatLng([a.lat, a.lon]);
        // Only rebuild the icon / tooltip when something actually changed —
        // rebinding every frame tears down an active hover and makes tooltips
        // flicker in and out.
        setIconIfChanged(entry, a.track, isSel, false);
        if (entry.label !== label) { entry.marker.setTooltipContent(label); entry.label = label; }
      }
      entry.marker.setOpacity(1);
      entry.marker.setZIndexOffset(isSel ? 1000 : 0);
    }
  }
  // absent aircraft: in live mode they linger as fading ghosts; in past mode
  // the reconstructed snapshot IS the truth at time T, so drop them at once.
  for (const icao of Object.keys(layers)) {
    if (seen.has(icao)) continue;
    const e = layers[icao];
    if (!live) { removeEntry(icao); continue; }
    if (e.ghostSince == null) e.ghostSince = now;
    const age = now - e.ghostSince;
    if (age > GHOST_TTL_MS) { removeEntry(icao); continue; }
    const isSel = icao === selected;
    const op = ghostFade(age);
    e.tracks.forEach(l => l.setStyle({ opacity: op, weight: isSel ? 5 : 3 }));
    if (e.marker) {
      setIconIfChanged(e, e.data ? e.data.track : 0, isSel, true);
      e.marker.setOpacity(Math.max(op, 0.25));
      e.marker.setZIndexOffset(isSel ? 1000 : 0);
    }
  }
  drawSelectionOverlays();
  renderList();
  drawPlot();
  updateHud();
}

function removeEntry(icao) {
  const e = layers[icao];
  if (!e) return;
  if (e.marker) map.removeLayer(e.marker);
  e.tracks.forEach(l => map.removeLayer(l));
  delete layers[icao];
}

// Concentric range rings from the receiver — the "radar" scale reference.
function drawRangeRings(rx) {
  if (ringsDrawn) return;
  ringsDrawn = true;
  for (const km of [25, 50, 75, 100]) {
    L.circle([rx.lat, rx.lon], { radius: km * 1000, fill: false,
      color: '#4be3e9', weight: 1.4, opacity: 0.6, dashArray: '6 6',
      interactive: false }).addTo(map);
    L.marker([rx.lat + (km * 1000) / 111320, rx.lon], {
      interactive: false,
      icon: L.divIcon({ className: 'ring-label',
        html: `<span style="display:block;width:36px;text-align:center;` +
              `color:#bfeef1;opacity:.9;font:9px ui-monospace,monospace;` +
              `text-shadow:0 0 3px #000,0 0 2px #000">${km}</span>`,
        iconSize: [36, 11], iconAnchor: [18, 6] }),
    }).addTo(map);
  }
}

// Line-of-sight to the selected aircraft + a marker at closest approach (the
// ground-track point where predicted Doppler crosses zero — the physical
// moment the plot's zero-crossing corresponds to).
function drawSelectionOverlays() {
  selLayers.forEach(l => map.removeLayer(l));
  selLayers = [];
  const found = selected && findAircraft(selected);
  if (!found || !receiverMarker) return;
  const a = found.a;
  // dark casing beneath the selected track so it reads clearly against a busy
  // sectional and other tracks; sent to the back so the colored segments draw
  // on top of it, leaving a thin dark outline on each side.
  const g = a.ground_track;
  if (g && g.length > 1) {
    const casing = L.polyline(g.map(p => [p[0], p[1]]),
      { color: '#05070b', weight: 11, opacity: 0.7, lineJoin: 'round',
        lineCap: 'round', interactive: false }).addTo(map);
    selLayers.push(casing);
    // Lift the whole selected track above every other track: casing to the
    // front first, then the colored segments on top of it (so the dark outline
    // hugs the coloured line). The marker is lifted via its z-index offset.
    casing.bringToFront();
    const sel = layers[selected];
    if (sel && sel.tracks) sel.tracks.forEach(l => l.bringToFront());
  }
  const rx = receiverMarker.getLatLng();
  if (a.lat != null) {
    selLayers.push(L.polyline([[rx.lat, rx.lng], [a.lat, a.lon]],
      { color: '#ffd400', weight: 1, opacity: 0.5, dashArray: '4 5',
        interactive: false }).addTo(map));
  }
  const ca = closestApproach(a.ground_track);
  if (ca) {
    selLayers.push(L.circleMarker(ca, { radius: 5, color: '#ffffff',
      weight: 2, fillColor: '#0c0f14', fillOpacity: 1, interactive: false })
      .addTo(map).bindTooltip('closest approach', { permanent: false }));
  }
}

// Interpolate the lat/lon where the Doppler sign flips (approaching->receding).
function closestApproach(g) {
  for (let i = 1; i < g.length; i++) {
    const d0 = g[i-1][2], d1 = g[i][2];
    if ((d0 >= 0) !== (d1 >= 0)) {
      const f = d0 === d1 ? 0.5 : d0 / (d0 - d1);
      return [g[i-1][0] + f * (g[i][0] - g[i-1][0]),
              g[i-1][1] + f * (g[i][1] - g[i-1][1])];
    }
  }
  return null;
}

// ---- aircraft rail -------------------------------------------------------
// Full aircraft card: callsign + confidence on the left, Doppler sparkline
// filling the rest. Used for live aircraft and the one expanded (selected) ghost.
function makeCard(a, ghost) {
  const row = el('div', 'row');
  if (a.icao === selected) row.classList.add('sel');
  if (ghost) row.classList.add('ghost');
  const info = el('div', 'info');
  info.append(el('span', 'cs', a.flight || a.icao),
              el('span', 'conf', `conf ${a.conf}`));
  const spark = el('canvas', 'spark');
  spark.width = 184; spark.height = 60;   // backing store; CSS scales down
  row.dataset.icao = a.icao;              // clicks handled by a delegated listener
  row.append(info, spark);
  requestAnimationFrame(() => drawSpark(spark, a));   // draw once flex width is known
  return row;
}

function renderList() {
  const div = document.getElementById('list');
  div.innerHTML = '';
  const liveSet = new Set(latest.aircraft.map(a => a.icao));
  const ghosts = [];
  for (const icao of Object.keys(layers)) {
    const e = layers[icao];
    if (!liveSet.has(icao) && e.ghostSince != null && e.data) ghosts.push(e.data);
  }
  // live aircraft: full cards, first-seen order (no reshuffle as conf updates)
  for (const a of latest.aircraft) div.appendChild(makeCard(a, false));
  // silent aircraft collapse to pills; the selected one expands to a full card
  const pills = ghosts.filter(a => a.icao !== selected);
  const openGhost = ghosts.find(a => a.icao === selected);
  if (openGhost) div.appendChild(makeCard(openGhost, true));
  if (pills.length) {
    const box = el('div', 'pillbox');
    for (const a of pills) {
      const pill = el('div', 'pill', a.flight || a.icao);
      pill.dataset.icao = a.icao;
      box.appendChild(pill);
    }
    div.appendChild(box);
  }
}

// Per-row Doppler sparkline: predicted curve colored by sign (blue<->red),
// echoing the map track — a glanceable pass signature.
function drawSpark(c, a) {
  const ctx = c.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const W = c.clientWidth || 92, H = c.clientHeight || 30, pad = 3;
  if (c.width !== Math.round(W*dpr) || c.height !== Math.round(H*dpr)) {
    c.width = Math.round(W*dpr); c.height = Math.round(H*dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const pts = a.doppler || [];
  if (pts.length < 2) return;
  const line = predictedLine(pts);         // interpolated, no stair-steps
  const ts = line.map(p => p.t), ps = line.map(p => p.y);
  const tmin = Math.min(...ts), tmax = Math.max(...ts);
  const lim = Math.max(60, Math.max(...ps.map(Math.abs)));  // symmetric about 0
  const sx = t => pad + (W - 2*pad) * (tmax === tmin ? 0.5 : (t - tmin)/(tmax - tmin));
  const sy = y => H/2 - (H/2 - pad) * Math.max(-1, Math.min(1, y / lim));
  ctx.strokeStyle = '#5f7186'; ctx.lineWidth = 1; ctx.setLineDash([2, 2]);  // 0 Hz centerline
  ctx.beginPath(); ctx.moveTo(pad, H/2); ctx.lineTo(W - pad, H/2); ctx.stroke();
  ctx.setLineDash([]);
  ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
  for (let i = 1; i < line.length; i++) {
    ctx.strokeStyle = dopColor(ps[i]);
    ctx.beginPath();
    ctx.moveTo(sx(ts[i-1]), sy(ps[i-1]));
    ctx.lineTo(sx(ts[i]), sy(ps[i]));
    ctx.stroke();
  }
}

// Rebuild a marker's icon only when its visible state changes (heading rounded
// to whole degrees, selection, ghosting). Recreating the divIcon DOM every
// frame is what made hover tooltips flicker.
function setIconIfChanged(entry, track, sel, ghost) {
  const key = `${Math.round(track || 0)}|${sel ? 1 : 0}|${ghost ? 1 : 0}`;
  if (entry.iconKey === key) return;
  entry.iconKey = key;
  entry.marker.setIcon(planeIcon(track, sel, ghost));
}

function planeIcon(track, sel, ghost) {
  const size = sel ? 38 : 28;
  // Selection wins over ghosting: a selected-but-silent aircraft still goes
  // gold + enlarged, so you can tell which quiet plane is selected.
  const fill = sel ? '#ffd400' : ghost ? '#9aa6b5' : '#ffffff';
  const stroke = sel ? '#5a4500' : ghost ? '#2a3340' : '#11151c';
  const rot = track || 0;
  const svg =
    `<svg width="${size}" height="${size}" viewBox="0 0 24 24" ` +
    `style="transform:rotate(${rot}deg);filter:drop-shadow(0 0 2px rgba(0,0,0,0.9))">` +
    `<path d="M12 2 L13.4 9 L22 13.2 L22 15 L13.4 12.4 L12.9 19 L15.5 20.6 L15.5 22 ` +
    `L12 21 L8.5 22 L8.5 20.6 L11.1 19 L10.6 12.4 L2 15 L2 13.2 L10.6 9 Z" ` +
    `fill="${fill}" stroke="${stroke}" stroke-width="1"/></svg>`;
  return L.divIcon({ html: svg, className: 'plane-icon',
                     iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
}

function select(icao) {
  selected = (selected === icao) ? null : icao;   // click again to deselect
  document.getElementById('rail').classList.toggle('selected', !!selected);
  render(latest);
}

function toggleRail() {
  const closed = document.getElementById('rail').classList.toggle('closed');
  document.getElementById('railToggle').classList.toggle('collapsed', closed);
}

// ---- measured-vs-predicted plot -----------------------------------------
function niceTicks(min, max, count) {
  if (!isFinite(min) || !isFinite(max) || min === max) return { step: 1, values: [min] };
  const step0 = (max - min) / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const n = step0 / mag;
  const step = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * mag;
  const values = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-6; v += step)
    values.push(v);
  return { step, values };
}

const COL = { grid: '#15202e', axis: '#38445a', zero: '#6b7c94', ink: '#7c8a9c',
              measured: '#7fb0ff', predicted: '#54c07a' };

// Compact Hz tick label: 1000 -> "1k", -1500 -> "-1.5k". Keeps the y-axis
// labels narrow so the plot can use more of the bar's width.
function fmtHz(v, dec) {
  if (Math.abs(v) >= 1000) {
    const k = v / 1000;
    return (Number.isInteger(k) ? k.toString() : k.toFixed(1)) + 'k';
  }
  return v.toFixed(dec);
}

// The predicted Doppler is only recomputed when a fresh position message
// arrives, so between updates (and across dropouts) its value is held flat —
// giving the curve a stair-step look. Keeping only the points where the value
// actually changes (plus the endpoints) turns each held run into a single
// straight interpolation between distinct values: a clean curve, no steps.
function predictedLine(pts) {
  const eps = 0.05, out = [];
  for (let i = 0; i < pts.length; i++) {
    if (i === 0 || i === pts.length - 1 ||
        Math.abs(pts[i].predicted - pts[i - 1].predicted) > eps)
      out.push({ t: pts[i].t, y: pts[i].predicted });
  }
  return out;
}

function drawPlot() {
  const c = document.getElementById('plot');
  if (!selected) return;
  const ctx = c.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const W = c.clientWidth || 600, H = c.clientHeight || 210;
  if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) {
    c.width = Math.round(W * dpr); c.height = Math.round(H * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const found = findAircraft(selected);
  const meta = document.getElementById('meta');
  if (!found) {
    ctx.fillStyle = COL.ink; ctx.font = '13px system-ui, sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('no data at this time', W / 2, H / 2);
    meta.replaceChildren(el('div', 'placeholder', 'no data at this time')); return;
  }
  const a = found.a, pts = a.doppler;
  const ts = pts.map(p => p.t), ms = pts.map(p => p.measured), ps = pts.map(p => p.predicted);
  const tmin = Math.min(...ts), tmax = Math.max(...ts);
  // Symmetric about 0 so the zero line sits dead-centre and it's immediately
  // clear which side (approaching / receding) each point falls on.
  const yabs = Math.max(1, ...ms.map(Math.abs), ...ps.map(Math.abs));
  const ymin = -yabs, ymax = yabs;
  const padL = 30, padR = 10, padT = 18, padB = 32;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const sx = t => padL + plotW * (tmax === tmin ? 0.5 : (t - tmin) / (tmax - tmin));
  const sy = y => padT + plotH * (ymax === ymin ? 0.5 : 1 - (y - ymin) / (ymax - ymin));

  ctx.font = '10px ui-monospace, monospace'; ctx.lineWidth = 1;
  const yTicks = niceTicks(ymin, ymax, Math.max(2, Math.floor(plotH / 40)));
  const yDec = yTicks.step < 1 ? 1 : 0;
  ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  for (const v of yTicks.values) {
    const y = Math.round(sy(v)) + 0.5;
    ctx.strokeStyle = COL.grid; ctx.beginPath();
    ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
    ctx.fillStyle = COL.ink; ctx.fillText(fmtHz(v, yDec), padL - 5, sy(v));
  }
  const xTicks = niceTicks(tmin, tmax, Math.max(2, Math.floor(plotW / 66)));
  const xDec = xTicks.step < 1 ? 1 : 0;
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  for (const v of xTicks.values) {
    const x = Math.round(sx(v)) + 0.5;
    ctx.strokeStyle = COL.grid; ctx.beginPath();
    ctx.moveTo(x, padT); ctx.lineTo(x, H - padB); ctx.stroke();
    ctx.fillStyle = COL.ink; ctx.fillText(v.toFixed(xDec), sx(v), H - padB + 4);
  }
  const yz = Math.round(sy(0)) + 0.5;   // zero line: finely dashed, brighter than grid
  ctx.strokeStyle = COL.zero; ctx.lineWidth = 1.2; ctx.setLineDash([2, 3]);
  ctx.beginPath(); ctx.moveTo(padL, yz); ctx.lineTo(W - padR, yz); ctx.stroke();
  ctx.setLineDash([]); ctx.lineWidth = 1;

  ctx.fillStyle = COL.ink; ctx.font = '11px system-ui, sans-serif';
  ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
  ctx.fillText('time (s)', padL + plotW / 2, H - 2);
  // y-axis title as a compact horizontal label top-left, instead of a rotated
  // title that would need a wide left margin.
  ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  ctx.fillText('Doppler (Hz)', 2, 8);

  // measured points first...
  ctx.fillStyle = COL.measured; ctx.strokeStyle = '#05070b';
  pts.forEach(p => { ctx.beginPath(); ctx.arc(sx(p.t), sy(p.measured), 2.6, 0, 7);
    ctx.fill(); ctx.stroke(); });
  // ...then the predicted curve ON TOP, with a dark casing, so it stays visible
  // even where the blue measured cloud is dense.
  const pl = predictedLine(pts);
  ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.globalAlpha = 0.8;  // let dots peek through
  for (const [color, w] of [['#05070b', 4], [COL.predicted, 2]]) {
    ctx.strokeStyle = color; ctx.lineWidth = w; ctx.beginPath();
    pl.forEach((p, i) => { const x = sx(p.t), y = sy(p.y);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.stroke();
  }
  ctx.globalAlpha = 1; ctx.lineWidth = 1;
  renderMeta(meta, a, found.ghostSince);
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function renderMeta(meta, a, ghostSince) {
  const trk = a.track != null ? Math.round(a.track) + '°' : '-';
  const range_mi = a.range_km != null ? (a.range_km * 0.621371).toFixed(1) : '-';
  const stats = [
    ['scale', a.scale], ['corr', a.corr], ['conf', a.conf], ['bursts', a.bursts],
    ['range', range_mi, a.range_km != null ? 'mi' : ''],
    ['alt', a.alt ?? '-', a.alt != null ? 'ft' : ''],
    ['spd', a.speed_kt ?? '-', a.speed_kt != null ? 'kt' : ''], ['trk', trk],
  ];
  const grid = el('div', 'stats');
  for (const [k, v, u] of stats) {
    const cell = el('div', 'stat');
    cell.append(el('span', 'k', k));
    const val = el('span', 'v', String(v));
    if (u) val.append(el('span', 'u', u));
    cell.append(val);
    grid.append(cell);
  }
  const head = el('div', 'callsign', a.flight || a.icao);
  const frag = [head];
  if (a.flight) frag.push(el('div', 'sub', a.icao));
  if (ghostSince) {
    const secs = Math.round((Date.now() - ghostSince) / 1000);
    frag.push(el('div', 'stale', `stale · last heard ${secs}s ago`));
  }
  frag.push(grid);
  meta.replaceChildren(...frag);
}

// ---- health strip --------------------------------------------------------
function hms(epoch) {
  const d = new Date(epoch * 1000);
  return d.toISOString().slice(11, 19);
}
function dur(secs) {
  secs = Math.max(0, Math.round(secs));
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60);
  return h ? `${h}h${String(m).padStart(2,'0')}` : `${m}m${String(secs%60).padStart(2,'0')}`;
}
function updateHud() {
  const r = latest.receiver || {};
  document.getElementById('h-rx').textContent =
    (r.lat != null) ? `${r.lat.toFixed(2)}, ${r.lon.toFixed(2)}` : '--';
  document.getElementById('h-count').innerHTML =
    `${latest.aircraft.length}<span class="u">ac</span>`;
  // clock lives in the bottom scrubber bar: absolute UTC of the current frame,
  // plus how far behind live when scrubbing.
  const t = mode === 'live' ? serverNow : viewTime;
  const utc = t ? hms(t) + 'Z' : '--:--:--';
  document.getElementById('clock').textContent = mode === 'live' ? utc
    : `${utc} −${dur((tl ? tl.end : serverNow) - viewTime)}`;
  const up = (tl && tl.start) ? serverNow - tl.start : 0;
  document.getElementById('h-uptime').innerHTML = `${dur(up)}<span class="u">up</span>`;
  let rate = 0;
  if (tl && tl.buckets && tl.buckets.length) {
    const w = (tl.end - tl.start) / tl.buckets.length || 1;
    rate = tl.buckets[tl.buckets.length - 1] / w;
  }
  document.getElementById('h-rate').innerHTML =
    `${rate.toFixed(1)}<span class="u">brst/s</span>`;
  const badge = document.getElementById('h-mode');
  badge.textContent = mode === 'live' ? 'LIVE' : 'PAST';
  badge.className = 'badge ' + (mode === 'live' ? 'live' : 'past');
}

// ---- timeline scrubber ---------------------------------------------------
const tlCanvas = document.getElementById('timeline');
function drawTimeline() {
  const c = tlCanvas, ctx = c.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const W = c.clientWidth || 400, H = c.clientHeight || 34;
  if (c.width !== Math.round(W*dpr) || c.height !== Math.round(H*dpr)) {
    c.width = Math.round(W*dpr); c.height = Math.round(H*dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (!tl || !tl.buckets || !tl.buckets.length) return;
  const n = tl.buckets.length, bw = W / n;
  const peak = Math.max(1, ...tl.buckets);
  for (let i = 0; i < n; i++) {
    const h = (tl.buckets[i] / peak) * (H - 4);
    ctx.fillStyle = tl.buckets[i] ? 'rgba(53,208,214,0.5)' : 'rgba(53,208,214,0.06)';
    ctx.fillRect(i * bw, H - h, Math.max(1, bw - 0.5), h);
  }
  positionPlayhead();
}
function positionPlayhead() {
  if (!tl) return;
  const span = (tl.end - tl.start) || 1;
  const t = mode === 'live' ? tl.end : viewTime;
  const frac = Math.max(0, Math.min(1, (t - tl.start) / span));
  document.getElementById('playhead').style.left = (frac * 100) + '%';
}
// pointer x on the strip -> epoch
function xToTime(clientX) {
  const rect = tlCanvas.getBoundingClientRect();
  const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  return tl.start + frac * (tl.end - tl.start);
}
let scrubbing = false;
const scrub = document.getElementById('scrub');
scrub.addEventListener('pointerdown', e => {
  if (!tl || !tl.recording) return;
  scrubbing = true; scrub.setPointerCapture(e.pointerId);
  playing = false; updateTransport();
  seek(xToTime(e.clientX));
});
scrub.addEventListener('pointermove', e => { if (scrubbing) seek(xToTime(e.clientX)); });
scrub.addEventListener('pointerup', e => { scrubbing = false; });

// ---- time-travel machine -------------------------------------------------
function goLive() {
  mode = 'live'; playing = false; speed = 1;   // returning to live resets speed
  document.getElementById('console').classList.remove('past');
  updateTransport(); positionPlayhead();
  fetchState(null);
}
function seek(t) {
  mode = 'past';
  viewTime = Math.max(tl.start, Math.min(tl.end, t));
  document.getElementById('console').classList.add('past');
  updateTransport(); positionPlayhead();
  fetchState(viewTime);
}
async function fetchState(at) {
  if (inflight) return;
  inflight = true;
  try {
    const url = at == null ? '/api/state' : `/api/state?at=${at}`;
    const r = await fetch(url);
    render(await r.json());
  } catch (e) { /* keep last frame */ }
  finally { inflight = false; }
}
async function pollTimeline() {
  try {
    const r = await fetch('/api/timeline');
    tl = await r.json();
    document.getElementById('transport').classList.toggle('norecord', !tl.recording);
    drawTimeline();
  } catch (e) { /* keep last strip */ }
}

// ---- transport controls --------------------------------------------------
function updateTransport() {
  document.getElementById('transport').classList.toggle('past', mode === 'past');
  document.getElementById('live').classList.toggle('on', mode === 'live');
  document.getElementById('playPause').textContent = playing ? '❚❚' : '▶';
  document.getElementById('speed').innerHTML = speed + '&times;';
  // clock text is owned by updateHud (it has the current-frame time)
}
document.getElementById('live').onclick = goLive;
document.getElementById('toStart').onclick = () => { if (tl) seek(tl.start); };
document.getElementById('speed').onclick = () => {
  speed = SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.length]; updateTransport();
};
document.getElementById('playPause').onclick = () => {
  if (!tl || !tl.recording) return;
  if (mode === 'live') seek(Math.max(tl.start, tl.end - 120));  // rewind 2 min
  playing = !playing; updateTransport();
};
document.getElementById('railToggle').onclick = toggleRail;
// Delegated so a click still selects even though renderList rebuilds the rows
// every second (a per-row handler can be torn out from under the pointer).
// pointerdown (not click): fires on press alone, so a selection can't be
// swallowed when the 1s list rebuild lands between mousedown and mouseup.
document.getElementById('list').addEventListener('pointerdown', e => {
  const hit = e.target.closest('[data-icao]');   // full card OR collapsed pill
  if (hit) select(hit.dataset.icao);
});

// playback tick: advance the virtual clock, snap back to live at the end
setInterval(() => {
  if (!playing || mode !== 'past' || !tl) return;
  const next = viewTime + speed * 0.25;
  if (next >= tl.end) { playing = false; goLive(); return; }
  viewTime = next;
  updateTransport(); positionPlayhead(); fetchState(viewTime);
}, 250);

// ---- keyboard ------------------------------------------------------------
const rail = document.getElementById('rail');
window.addEventListener('keydown', e => {
  if (e.key === ' ') { e.preventDefault(); document.getElementById('playPause').click(); }
  else if (e.key.toLowerCase() === 'l') goLive();
  else if (e.key === 'Escape') select(selected);
  else if (e.key === 'Tab') { e.preventDefault(); toggleRail(); }
  else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') cycle(e.key === 'ArrowRight' ? 1 : -1);
});
function cycle(dir) {
  const ids = latest.aircraft.map(a => a.icao);
  if (!ids.length) return;
  const i = ids.indexOf(selected);
  const next = ids[(i + dir + ids.length) % ids.length];
  if (selected !== next) select(next);
}

// ---- main loop -----------------------------------------------------------
fetchState(null);
pollTimeline();
setInterval(() => { if (mode === 'live') fetchState(null); }, 1000);
setInterval(pollTimeline, 2000);
window.addEventListener('resize', () => { drawPlot(); drawTimeline(); });
