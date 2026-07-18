// doppler1090 dashboard.
//
// The map is a fullscreen "scope"; a bottom console carries the time-travel
// scrubber and (when an aircraft is selected) the measured-vs-predicted plot.
// Live capture never stops server-side — scrubbing into the past is purely a
// read: the client either polls /api/state (live) or /api/state?at=T (past),
// while /api/timeline keeps the activity strip growing either way.

// The FAA chart cache only has native tiles z8-z12; outside that the service
// 404s. detectRetina pulls one level deeper, so map-zoom N requests z(N+1) and
// knocks a layer's effective maxZoom down by 1. Map-zoom 11 already shows native
// z12. Map-zoom 12 is one extra notch: the chart layers set maxNativeZoom 11 so
// the retina request clamps back to native z12 and upscales (blurrier, no new
// detail) instead of blanking, and maxZoom 13 so they stay selectable in the
// layer picker at map-zoom 12 (13 - 1 retina = 12) rather than greying out.
const map = L.map('map', { minZoom: 7, maxZoom: 12, zoomControl: false });
// Zoom control on the right, by the layer picker — clear of the health strip.
L.control.zoom({ position: 'topright' }).addTo(map);
// Center a point in the *visible* map area — clear of the left panel (~320 px)
// and the bottom console (~84 px) — rather than the geometric centre.
function centerOnVisible(lat, lon, zoom) {
  const z = zoom == null ? map.getZoom() : zoom;
  if (window.innerWidth <= 760) { map.setView([lat, lon], z); return; }
  // Put the point at the centre of the *visible* map area (right of the ~320px
  // panel, above the ~84px console) by computing the map centre directly: the
  // centre must sit so the point projects to the visible-area centre pixel.
  const size = map.getSize();
  const dx = (320 + size.x) / 2 - size.x / 2;   // = 160, shift right
  const dy = (size.y - 84) / 2 - size.y / 2;    // = -42, shift up
  const p = map.project([lat, lon], z);
  map.setView(map.unproject(p.subtract([dx, dy]), z), z);
}
centerOnVisible(41.978, -87.904, 10);   // O'Hare placeholder; recenters on receiver
const osm = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  { maxZoom: 19, detectRetina: true, attribution: '© OpenStreetMap' });
const faaChart = name => L.tileLayer(
  `https://tiles.arcgis.com/tiles/ssFJjBXIUyZDrSYZ/arcgis/rest/services/${name}/MapServer/tile/{z}/{y}/{x}`,
  { maxZoom: 13, minNativeZoom: 8, maxNativeZoom: 11, opacity: 0.4,
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
// Antenna coverage: a toggleable overlay (off by default) drawing the farthest-
// heard range per bearing as a polygon around the receiver.
const coverageLayer = L.layerGroup();
coverageLayer.on('add', () => updateCoverage(latest.coverage));
L.control.layers(baseLayers, { 'Coverage': coverageLayer },
                 { position: 'topright' }).addTo(map);
// Clicks that miss a marker (empty space, or just off a cluster) still route
// through the same cycle logic; trails are non-interactive so they pass through.
map.on('click', ev => selectAt(ev.containerPoint));

// ---- state ---------------------------------------------------------------
let receiverMarker = null, ringsDrawn = false;
let layers = {};              // icao -> { marker, tracks:[polyline], data, ghostSince }
let selLayers = [];           // line-of-sight + closest-approach overlays
let selected = null;
let pendingFit = null;        // icao to reveal on the map after this render (fresh autoselect)
let pendingFly = null;        // icao to fly in close to after this render (record replay)
let latest = { aircraft: [] };
let serverNow = 0;            // most recent server_time seen
let hadAircraft = false;      // did the last live frame have any aircraft?

let mode = 'live';            // 'live' | 'past'
let viewTime = 0;             // epoch of the current past frame
let playing = false, speed = 1;
const SPEEDS = [1, 2, 5, 10, 20];   // playback multipliers cycled by the button
let tl = null;                // { start, end, buckets, recording }
let isReplay = false;         // replay: auto-play + loop, no true live frame
let viewSession = null;       // basename of a past session being replayed (null = live/home)
let inflight = false;         // guards overlapping /api/state fetches

const GHOST_TTL_MS = 5 * 60 * 1000;
function ghostFade(ageMs) { return 0.5 - 0.38 * Math.min(1, ageMs / GHOST_TTL_MS); }

// Doppler (Hz) -> diverging color. + = approaching = blue, - = receding = red.
// Blue (approaching) -> white (0) -> red (receding). Optional `mute` (0..1)
// blends toward slate, used to fade ghost trails by desaturating rather than
// going transparent (transparent overlaps brighten where segments meet).
function dopColor(hz, mute) {
  const x = Math.max(-1, Math.min(1, hz / 600));
  let r, g, b;
  if (x >= 0) { const t = x; r = Math.round(255*(1-t)); g = r; b = 255; }
  else { const t = -x; r = 255; g = Math.round(255*(1-t)); b = g; }
  if (mute) { const s = [92, 102, 116];   // slate the colours fade toward
    r = Math.round(r + (s[0]-r)*mute);
    g = Math.round(g + (s[1]-g)*mute);
    b = Math.round(b + (s[2]-b)*mute); }
  return `rgb(${r},${g},${b})`;
}

// Paint one trail segment as a Doppler gradient. Predicted Doppler is a smooth
// function of position, so we subdivide the segment and colour each slice by the
// value interpolated between the two endpoints. This makes the blue->white->red
// transition land in the right place even on a long, coarsely-sampled segment
// (e.g. across an overhead pass, or a position gap) where a single endpoint
// colour would paint the whole thing one side of the flip. Short/flat segments
// collapse to a single slice, so this stays cheap for the common case.
function addTrailSegment(p0, p1, weight, sink) {
  const steps = Math.max(1, Math.min(12, Math.round(Math.abs(p1[2] - p0[2]) / 40)));
  for (let k = 0; k < steps; k++) {
    const f0 = k / steps, f1 = (k + 1) / steps, fm = (f0 + f1) / 2;
    const dop = p0[2] + (p1[2] - p0[2]) * fm;
    const seg = L.polyline([
      [p0[0] + (p1[0] - p0[0]) * f0, p0[1] + (p1[1] - p0[1]) * f0],
      [p0[0] + (p1[0] - p0[0]) * f1, p0[1] + (p1[1] - p0[1]) * f1]],
      // Fully opaque so overlapping round caps don't brighten where slices meet
      // (the line is thin, so the chart still reads around it); round caps + join
      // keep turns gap-free. Ghosts fade by muting colour, not lowering opacity.
      { color: dopColor(dop), weight, opacity: 1,
        lineCap: 'round', lineJoin: 'round', interactive: false });
    seg._dop = dop;                    // remembered so ghosts recolour muted
    seg.addTo(map); sink.push(seg);
  }
}

// Colour the "doppler1090" logo per-character as a real Doppler pass at ~10 mi
// closest approach: the radial-velocity S-curve f_d ∝ -x/√(d²+x²) sampled across
// the letters, run through the same dopColor scale (blue -> white -> red).
function buildLogo() {
  const text = 'doppler1090', N = text.length, d = 20, A = 18;   // 20 mi pass
  const brand = document.querySelector('.brand');
  if (!brand) return;
  brand.replaceChildren(...[...text].map((ch, i) => {
    const x = -A + 2 * A * i / (N - 1);
    // amplitude kept under the 600 Hz saturation point so the ends stay a soft
    // blue/red rather than fully saturated.
    const s = document.createElement('span');
    s.textContent = ch;
    s.style.color = dopColor(540 * (-x / Math.sqrt(d * d + x * x)));
    return s;
  }));
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
  // auto-select the first aircraft when the live list goes empty -> populated
  // (startup, or all dropped then a new one appears), so details show without a
  // manual first click. Keyed off that transition so it never fights a deselect.
  const hasSel = selected != null && state.aircraft.some(a => a.icao === selected);
  if (mode === 'live' && !hasSel && state.aircraft.length && !hadAircraft) {
    selected = state.aircraft[0].icao;
    document.getElementById('rail').classList.add('selected');
    pendingFit = selected;   // fresh pick: reveal it once markers are placed
  }
  if (mode === 'live') hadAircraft = state.aircraft.length > 0;
  if (!receiverMarker && state.receiver && state.receiver.lat != null) {
    receiverMarker = L.circleMarker([state.receiver.lat, state.receiver.lon],
      { radius: 7, color: '#000', weight: 2, fillColor: '#ffd400', fillOpacity: 1 })
      .addTo(map).bindTooltip('receiver');
    centerOnVisible(state.receiver.lat, state.receiver.lon);  // visible-area centre
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
      addTrailSegment(g[i-1], g[i], isSel ? 7 : 4, entry.tracks);
    }
    if (a.lat != null) {
      if (!entry.marker) {
        entry.marker = L.marker([a.lat, a.lon],
          { icon: planeIcon(a.track, isSel, false, shapeFor(a)) })
          // A click cycles through every aircraft under the pointer (selectAt),
          // so overlapping planes in busy areas all stay reachable.
          .addTo(map).on('click', ev => selectAt(ev.containerPoint));
      } else {
        entry.marker.setLatLng([a.lat, a.lon]);
        setIconIfChanged(entry, a.track, isSel, false, shapeFor(a));
      }
      entry.marker.setOpacity(1);
      // Live icons stack by altitude (highest on top); selected always tops.
      entry.marker.setZIndexOffset(zFor(isSel, a.alt, true));
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
    // A ghost trail stays fully opaque and fades by muting its colour toward
    // slate (keeps it artifact-free, unlike transparency). A selected ghost
    // stays vivid so you can tell which quiet aircraft is selected.
    const mute = isSel ? 0 : 0.5 + 0.3 * Math.min(1, age / GHOST_TTL_MS);
    e.tracks.forEach(l => l.setStyle({
      color: dopColor(l._dop || 0, mute), opacity: 1, weight: isSel ? 5 : 3 }));
    if (e.marker) {
      setIconIfChanged(e, e.data ? e.data.track : 0, isSel, true, shapeFor(e.data || {}));
      e.marker.setOpacity(isSel ? 1 : Math.max(op, 0.25));
      e.marker.setZIndexOffset(zFor(isSel, e.data && e.data.alt, false));
    }
  }
  drawSelectionOverlays();
  renderList();
  drawPlot();
  updateHud();
  refreshRecord();
  // markers now placed: reveal a freshly auto-selected plane if it's off-screen
  if (pendingFit != null) { maybeFit(pendingFit); pendingFit = null; }
  // record replay: fly in close on the record-setting plane once it's on the map
  if (pendingFly != null) {
    const d = layers[pendingFly] && layers[pendingFly].data;
    if (d && d.lat != null) flyToPlane(d.lat, d.lon);
    pendingFly = null;   // one-shot: attempt once, never linger onto a later frame
  }
}

function removeEntry(icao) {
  const e = layers[icao];
  if (!e) return;
  if (e.marker) map.removeLayer(e.marker);
  e.tracks.forEach(l => map.removeLayer(l));
  delete layers[icao];
  if (selected === icao) {          // dropped the selected aircraft: deselect
    selected = null;
    document.getElementById('rail').classList.remove('selected');
  }
}

// Concentric range rings from the receiver, in nautical miles (aviation
// standard, matching kt/ft) — the "radar" scale reference.
const NM_TO_M = 1852;
function drawRangeRings(rx) {
  if (ringsDrawn) return;
  ringsDrawn = true;
  // 5 and 10 nm give a close-in scale for overhead / nearby traffic; the rest
  // step out to the horizon.
  for (const nm of [5, 10, 25, 50, 75, 100, 150, 200, 250]) {
    const r = nm * NM_TO_M;
    L.circle([rx.lat, rx.lon], { radius: r, fill: false,
      color: '#4be3e9', weight: 1.4, opacity: 0.6, dashArray: '6 6',
      interactive: false }).addTo(map);
    L.marker([rx.lat + r / 111320, rx.lon], {
      interactive: false,
      icon: L.divIcon({ className: 'ring-label',
        html: `<span style="display:block;width:48px;text-align:center;` +
              `color:#bfeef1;opacity:.9;font:9px ui-monospace,monospace;` +
              `text-shadow:0 0 3px #000,0 0 2px #000">${nm} nm</span>`,
        iconSize: [48, 11], iconAnchor: [24, 6] }),
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
// Make/model text from the snapshot (resolved + cached server-side; empty
// until it resolves, or if the aircraft isn't in any database).
function typeLabel(a) {
  return [a.make, a.model].filter(Boolean).join(' ');
}

// One dense scan row: flight · type · tiny Doppler spark · altitude · range.
// The spark dims on a low-confidence fit, folding trust into the trend you're
// already reading. Used for live aircraft and (dimmed) for ghosts.
function makeCard(a, ghost) {
  const row = el('div', 'row');
  if (a.icao === selected) row.classList.add('sel');
  if (ghost) row.classList.add('ghost');
  row.dataset.icao = a.icao;              // clicks handled by a delegated listener
  row.append(el('span', 'cs', a.flight || a.icao));
  row.append(el('span', 'type', a.type || a.model || a.make || ''));
  const spark = el('canvas', 'spark');
  if ((a.conf ?? 1) < 0.4) spark.classList.add('dim');   // weak fit -> faded trend
  row.append(spark);
  row.append(el('span', 'alt', a.alt != null      // flight level: alt/100, 3-digit
    ? String(Math.round(a.alt / 100)).padStart(3, '0') : '–'));
  const nm = a.range_km ? a.range_km / 1.852 : null;     // km -> nautical miles
  row.append(el('span', 'rng',
    nm == null ? '–' : nm.toFixed(2)));
  requestAnimationFrame(() => drawSpark(spark, a));   // draw once the cell has width
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
  if (!latest.aircraft.length && !ghosts.length) {   // nothing decoded yet
    div.appendChild(el('div', 'listwait', 'waiting for ADS-B data…'));
    return;
  }
  div.appendChild(listHead());   // column labels, pinned while the list scrolls
  // live aircraft first (first-seen order, no reshuffle), then silent ones as
  // dimmed rows below - kept in the same scannable list, no separate pills.
  for (const a of latest.aircraft) div.appendChild(makeCard(a, false));
  for (const a of ghosts) div.appendChild(makeCard(a, true));
}

// Sticky column-header row; same grid as the data rows so the labels line up.
// Short/unit labels (rows carry no units) with a hover tooltip explaining each.
function listHead() {
  const h = el('div', 'row head');
  const cols = [
    ['flight', 'Callsign, or tail number for general aviation'],
    ['type', 'Aircraft type (ICAO type code, e.g. E75L)'],
    ['Doppler', 'Measured Doppler trend over the pass - blue approaching, red receding'],
    ['FL', 'Flight level - altitude in hundreds of feet (FL370 = 37,000 ft)'],
    ['range', 'Straight-line distance from your receiver, in nautical miles'],
  ];
  for (const [t, help] of cols) {
    const s = el('span', null, t);
    s.title = help;
    h.appendChild(s);
  }
  return h;
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
// make/model -> [tar1090 shape name, scale], used only when the exact ICAO
// type designator isn't known.
function keywordShape(a) {
  const t = ((a && a.make || '') + ' ' + (a && a.model || '')).toUpperCase();
  const has = (...w) => w.some(x => t.includes(x));
  if (has('HELICOPTER', 'SIKORSKY', 'ROBINSON', 'EUROCOPTER', 'BELL ', 'AS35',
          'EC13', 'EC17', 'R44', 'R66', 'H125', 'H130')) return ['helicopter', 1];
  if (has('747', 'A380', 'A340', 'A400', 'IL-96')) return ['heavy_4e', 1];
  if (has('777', '787', 'A350', 'A330', '767', 'MD-11', 'A300', 'DC-10',
          'L-1011')) return ['heavy_2e', 1.05];
  if (has('CRJ', 'ERJ', 'EMB-1', 'E170', 'E175', 'E190', 'E195', 'RJ'))
    return ['twin_small', 1];
  if (has('737', 'A320', 'A319', 'A321', 'A318', 'A220', '757', '727',
          'MD-8', 'MD-9', 'DC-9', 'BCS')) return ['airliner', 1];
  if (has('ATR', 'DASH', 'DHC', 'DH8', 'SAAB', 'KING AIR', 'METRO'))
    return ['twin_large', 0.95];
  if (has('PC-12', 'C208', 'CARAVAN', 'TBM', 'PILATUS')) return ['single_turbo', 1];
  // Textron/Cessna/Beechcraft appear in the FAA registry as the corporate make
  // plus a bare model number, so decide by the model: 500-799 are Citation
  // jets, 208 a Caravan, 90-350 King Air turboprops, the rest light pistons.
  if (has('TEXTRON', 'CESSNA', 'BEECH', 'HAWKER')) {
    const model = (a && a.model || '').toUpperCase();
    if (has('CITATION', 'HAWKER') || /[567]\d\d/.test(model)) return ['hi_perf', 0.95];
    if (has('CARAVAN') || /\b208/.test(model)) return ['single_turbo', 1];
    if (has('KING AIR') || /\b[ABCEF]?(90|100|200|300|350)/.test(model))
      return ['twin_large', 0.95];
    return ['cessna', 1];
  }
  if (has('PIPER', 'CIRRUS', 'MOONEY', 'DIAMOND', 'C172',
          'C182', 'C152', 'PA-', 'SR2', 'BONANZA')) return ['cessna', 1];
  if (has('GULFSTREAM', 'LEARJET', 'CITATION', 'CHALLENGER', 'FALCON',
          'HAWKER', 'GLOBAL', 'PHENOM')) return ['hi_perf', 0.95];
  return ['airliner', 1];
}

// Prefer the exact ICAO type designator (tar1090 map), else the keyword fallback.
function shapeFor(a) {
  const m = (a && a.type && typeof AC_TYPE_ICONS !== 'undefined' &&
             AC_TYPE_ICONS[a.type]) || keywordShape(a || {});
  return { name: m[0], scale: m[1] || 1 };
}

function setIconIfChanged(entry, track, sel, ghost, info) {
  const key = `${Math.round(track || 0)}|${sel ? 1 : 0}|${ghost ? 1 : 0}|` +
    `${info ? info.name + info.scale : ''}`;
  if (entry.iconKey === key) return;
  entry.iconKey = key;
  entry.marker.setIcon(planeIcon(track, sel, ghost, info));
}

// The rotation pivot must be the plane's own centre, not the viewBox centre:
// a few shapes (airliner, cessna, glider) draw the silhouette slightly off
// centre, so rotating about the box centre swings the plane off the anchor by
// a few px in a heading-dependent direction, detaching it from its trail.
// Measure each path's bounding box once (via getBBox) and cache by path.
const _shapeCtr = {};
let _measSvg = null;
function shapeCenter(shp) {
  if (shp.path in _shapeCtr) return _shapeCtr[shp.path];
  const vb = shp.viewBox.split(/\s+/).map(Number);
  let c = { cx: vb[0] + vb[2] / 2, cy: vb[1] + vb[3] / 2 };   // viewBox-centre fallback
  try {
    if (!_measSvg) {
      _measSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      _measSvg.setAttribute('style',
        'position:absolute;left:-9999px;width:0;height:0;overflow:hidden');
      document.body.appendChild(_measSvg);
    }
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', shp.path);
    _measSvg.appendChild(p);
    const b = p.getBBox();
    _measSvg.removeChild(p);
    c = { cx: b.x + b.width / 2, cy: b.y + b.height / 2 };
  } catch (e) { /* keep viewBox-centre fallback */ }
  _shapeCtr[shp.path] = c;
  return c;
}

function planeIcon(track, sel, ghost, info) {
  const shp = (AC_SHAPES[info && info.name]) || AC_SHAPES.airliner;
  const k = (sel ? 1.6 : 1.2) * (info ? info.scale : 1);   // display scale
  const w = Math.round(shp.w * k), h = Math.round(shp.h * k);
  // Anchor + rotate about the silhouette's true centre (see shapeCenter). For
  // the ~centred majority this is the box centre; off-centre shapes get pinned.
  const vb = shp.viewBox.split(/\s+/).map(Number);
  const c = shapeCenter(shp);
  let ax = (c.cx - vb[0]) / vb[2] * w, ay = (c.cy - vb[1]) / vb[3] * h;
  if (!Number.isFinite(ax) || !Number.isFinite(ay)) { ax = w / 2; ay = h / 2; }
  // Selection wins over ghosting: a selected-but-silent aircraft still goes
  // gold + enlarged, so you can tell which quiet plane is selected.
  const fill = sel ? '#ffd400' : ghost ? '#9aa6b5' : '#ffffff';
  const stroke = sel ? '#5a4500' : ghost ? '#2a3340' : '#11151c';
  const rot = shp.noRotate ? 0 : (track || 0);
  const svg =
    `<svg width="${w}" height="${h}" viewBox="${shp.viewBox}" ` +
    `style="transform:rotate(${rot}deg);transform-origin:${ax}px ${ay}px;` +
    `filter:drop-shadow(0 0 2px rgba(0,0,0,0.9))">` +
    // strokeScale is the outline weight in viewBox units. The small silhouette
    // shapes (~20 wide) omit it; default to a hairline 1 - the old default of
    // 16 was sized for the big-viewBox airliners and floods small shapes solid.
    `<path d="${shp.path}" fill="${fill}" stroke="${stroke}" ` +
    `stroke-width="${shp.strokeScale || 1}"/></svg>`;
  return L.divIcon({ html: svg, className: 'plane-icon',
                     iconSize: [w, h], iconAnchor: [ax, ay] });
}

function select(icao) {
  const prev = selected;
  selected = (selected === icao) ? null : icao;   // click again to deselect
  document.getElementById('rail').classList.toggle('selected', !!selected);
  if (selected && selected !== prev) maybeFit(selected);   // new pick: reveal it
  render(latest);
}

// A map/marker click cycles through every aircraft under (or very near) the
// click, so overlapping planes in a busy area are all reachable. The first click
// on a stack takes the topmost live aircraft (highest altitude); each further
// click in the same spot steps to the next one underneath, wrapping around.
// Clicking empty space clears the selection.
const CLICK_RADIUS = 30;   // px around the click that counts as "under it"
function selectAt(pt) {
  const liveSet = new Set(latest.aircraft.map(a => a.icao));
  const cands = Object.values(layers)
    .filter(e => e.marker && e.data)
    .map(e => ({ icao: e.data.icao, live: liveSet.has(e.data.icao),
                 alt: e.data.alt || 0,
                 d: map.latLngToContainerPoint(e.marker.getLatLng()).distanceTo(pt) }))
    .filter(x => x.d <= CLICK_RADIUS)
    // topmost first: live before ghost, then highest altitude, then nearest
    .sort((p, q) => (q.live - p.live) || (q.alt - p.alt) || (p.d - q.d));
  if (!cands.length) {                 // empty space -> deselect
    if (selected) {
      selected = null;
      document.getElementById('rail').classList.remove('selected');
      render(latest);
    }
    return;
  }
  const i = cands.findIndex(x => x.icao === selected);
  const prev = selected;
  selected = cands[(i + 1) % cands.length].icao;   // i<0 -> topmost; else descend
  document.getElementById('rail').classList.add('selected');
  if (selected !== prev) maybeFit(selected);       // new pick: reveal if off-screen
  render(latest);
}

// ---- reveal the selected aircraft when it's off the visible map ----------
// Selecting a plane (by click or a fresh autoselect) should never leave it
// hidden behind the panel or off-view. If it's already visible we don't move.
const FIT_FLOOR_Z = 8;      // don't zoom out past the FAA chart-detail floor
const FIT_MARGIN = 0.14;    // keep the framed pair clear of the very edges
const FIT_MS = 600;         // flyTo ease duration

// Is (lat,lon) inside the *visible* map rect — right of the ~320px panel and
// above the ~84px console? On mobile the map fills the screen (sheet floats
// over it), so the whole map counts.
function isOnScreen(lat, lon) {
  const p = map.latLngToContainerPoint([lat, lon]);
  const size = map.getSize();
  const narrow = window.innerWidth <= 760;
  const left = narrow ? 0 : 320, bottom = narrow ? 0 : 84;
  return p.x >= left && p.x <= size.x && p.y >= 0 && p.y <= size.y - bottom;
}

// Ease the map so both the aircraft and the receiver sit inside the visible
// rect. Caps: never zoom out past the chart floor (fall back to centring the
// plane alone), never zoom in past the current view.
function flyToFit(lat, lon) {
  if (!receiverMarker) return;
  const size = map.getSize();
  if (window.innerWidth <= 760) {              // map behind the sheet: just centre
    const z = Math.max(FIT_FLOOR_Z, Math.min(map.getZoom(), 11));
    map.flyTo([lat, lon], z, { duration: FIT_MS / 1000 });
    return;
  }
  const a = L.latLng(lat, lon), b = receiverMarker.getLatLng();
  const vw = (size.x - 320) * (1 - FIT_MARGIN);   // visible-rect span, minus margin
  const vh = (size.y - 84) * (1 - FIT_MARGIN);
  // largest zoom (never above current) at which the pair spans within the rect
  let z = Math.min(map.getZoom(), map.getMaxZoom());
  for (; z > FIT_FLOOR_Z; z--) {
    const pa = map.project(a, z), pb = map.project(b, z);
    if (Math.abs(pa.x - pb.x) <= vw && Math.abs(pa.y - pb.y) <= vh) break;
  }
  const pa = map.project(a, z), pb = map.project(b, z);
  const fits = Math.abs(pa.x - pb.x) <= vw && Math.abs(pa.y - pb.y) <= vh;
  // fits -> centre on the pair's midpoint; floor hit -> frame the plane alone
  const mid = fits ? pa.add(pb).divideBy(2) : pa;
  const dx = (320 + size.x) / 2 - size.x / 2;   // +160, clear the panel
  const dy = (size.y - 84) / 2 - size.y / 2;    // -42, clear the console
  map.flyTo(map.unproject(mid.subtract([dx, dy]), z), z, { duration: FIT_MS / 1000 });
}

// Fly in *close* on one aircraft, centred in the visible rect at one below max
// zoom (record replay: you want to study that plane, not fit the whole geometry).
function flyToPlane(lat, lon) {
  const z = Math.max(map.getMinZoom(), map.getMaxZoom() - 1);
  if (window.innerWidth <= 760) {
    map.flyTo([lat, lon], z, { duration: FIT_MS / 1000 });
    return;
  }
  const size = map.getSize();
  const dx = (320 + size.x) / 2 - size.x / 2;   // +160, clear the panel
  const dy = (size.y - 84) / 2 - size.y / 2;    // -42, clear the console
  const p = map.project([lat, lon], z);
  map.flyTo(map.unproject(p.subtract([dx, dy]), z), z, { duration: FIT_MS / 1000 });
}

// Reveal `icao` only if it has a position and is currently off-screen.
function maybeFit(icao) {
  const d = layers[icao] && layers[icao].data;
  if (!d || d.lat == null) return;
  if (!isOnScreen(d.lat, d.lon)) flyToFit(d.lat, d.lon);
}

// Marker stacking: selected on top of everything, then live aircraft by altitude
// (highest highest), then ghosts sunk beneath all live planes.
function zFor(isSel, alt, live) {
  if (isSel) return 100000;
  const a = Math.round((alt || 0) / 10);
  return live ? a : a - 100000;
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
  if (!selected) {
    document.getElementById('meta').replaceChildren(
      el('div', 'placeholder', 'select an aircraft above to see details'));
    return;
  }
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
  if (!found) {                     // selected aircraft has gone: fall back cleanly
    selected = null;
    document.getElementById('rail').classList.remove('selected');
    meta.replaceChildren(
      el('div', 'placeholder', 'select an aircraft above to see details'));
    return;
  }
  const a = found.a, pts = a.doppler;
  const ts = pts.map(p => p.t), ms = pts.map(p => p.measured), ps = pts.map(p => p.predicted);
  const tmin = Math.min(...ts), tmax = Math.max(...ts);
  // Symmetric about 0 so the zero line sits dead-centre. Robust scale: the
  // predicted curve is always physical, but the measured carrier estimate goes
  // wild on weak signals (spurious FFT peaks). Cap the range so a few garbage
  // bursts don't dwarf everything — an 85th-percentile of |measured| plus a
  // hard cap; off-scale points get clamped to the plot edge (a visible "rail"
  // that flags an untrustworthy measurement rather than exploding the axis).
  const CAP = 3000;   // Hz — real Doppler is well under this
  const measAbs = ms.map(Math.abs).sort((a, b) => a - b);
  const p85 = measAbs.length ? measAbs[Math.floor(measAbs.length * 0.85)] : 0;
  const yabs = Math.max(300, Math.min(CAP, Math.max(p85, ...ps.map(Math.abs))));
  const ymin = -yabs, ymax = yabs;
  const padL = 30, padR = 10, padT = 18, padB = 32;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const sx = t => padL + plotW * (tmax === tmin ? 0.5 : (t - tmin) / (tmax - tmin));
  const sy = y => {   // clamp off-scale points to the plot edge
    const c = Math.max(ymin, Math.min(ymax, y));
    return padT + plotH * (ymax === ymin ? 0.5 : 1 - (c - ymin) / (ymax - ymin));
  };

  ctx.font = '10px ui-monospace, monospace'; ctx.lineWidth = 1;
  let yTicks = niceTicks(ymin, ymax, Math.max(2, Math.floor(plotH / 40)));
  // a symmetric range can snap to a step wider than the half-range, leaving only
  // the lone 0 tick — recompute finer so there's always a real scale.
  if (yTicks.values.length < 3) yTicks = niceTicks(ymin, ymax, 6);
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
  renderStats(document.getElementById('stats'), a);
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// Plain-English hover explanations for the detail-panel stat cells.
const STAT_HELP = {
  range: 'Straight-line distance from your receiver to the aircraft.',
  alt: 'Barometric altitude, in feet.',
  spd: 'Ground speed, in knots.',
  trk: 'Direction of travel (degrees, 0° = north).',
  scale: 'Measured-to-predicted Doppler ratio. A clean pass sits near 1.0; '
       + 'far off means the fit is poor or the geometry is unfavourable.',
  corr: 'How well the measured Doppler curve matches the shape predicted from '
      + "the aircraft's reported track (1.0 = perfect match, 0 = none).",
  conf: 'Overall confidence that this Doppler measurement is trustworthy (0–1), '
      + 'combining correlation, scale, and how straight/long the pass was.',
  bursts: 'Number of individual Doppler measurements collected on this pass.',
  sig: 'Received signal strength at your antenna, in dBFS. Closer to 0 is '
     + 'stronger (e.g. −24 is a stronger signal than −40).',
};

const _safeUrl = u => /^https?:\/\//i.test(u || '') ? u : '';   // http(s)-only sink guard

// Identity block: a larger inline photo of the tail (source thumbnails are only
// so big, so show it at full panel width rather than hiding it behind a click),
// then callsign, type · registration, and a stale line for ghosts. The plot and
// stats strip render below (renderStats), keeping the plot near the top.
function renderMeta(meta, a, ghostSince) {
  const frag = [el('div', 'callsign', a.flight || a.icao)];   // identity leads
  const model = [typeLabel(a), a.reg].filter(Boolean).join(' · ');
  if (model) frag.push(el('div', 'model', model));
  if (ghostSince) {
    const secs = Math.round((Date.now() - ghostSince) / 1000);
    frag.push(el('div', 'stale', `stale · last heard ${secs}s ago`));
  }
  // media row: the photo thumbnail (if any) sits beside the live telemetry
  const media = el('div', 'media');
  if (a.photo) {
    const fig = el('div', 'photo');
    fig.dataset.full = _safeUrl(a.photo);       // tap the thumb -> lightbox
    fig.dataset.link = _safeUrl(a.photo_link);
    fig.dataset.by = a.photo_by || '';
    const img = document.createElement('img');
    img.src = _safeUrl(a.photo); img.alt = a.flight || a.icao; img.loading = 'lazy';
    img.onerror = () => fig.remove();
    fig.append(img);
    const link = _safeUrl(a.photo_link);        // attribution (required by sources)
    if (link || a.photo_by) {
      const cr = el('div', 'credit');
      if (link) {
        const alink = document.createElement('a');
        alink.href = link; alink.target = '_blank'; alink.rel = 'noopener';
        alink.textContent = '© ' + (a.photo_by || 'source');   // untrusted -> textContent
        cr.appendChild(alink);
      } else {
        cr.textContent = '© ' + a.photo_by;
      }
      fig.append(cr);
    }
    media.append(fig);
  }
  media.append(kinStrip(a));                    // telemetry beside the thumbnail
  frag.push(media);
  meta.replaceChildren(...frag);
}

function _statChip(cls, txt, help) {
  const s = el('span', 'st' + (cls ? ' ' + cls : ''), txt);
  if (help && STAT_HELP[help]) s.title = STAT_HELP[help];
  return s;
}

// Live telemetry (range/alt/spd/trk) as a compact label/value list beside the
// photo thumbnail - colour-coded to the map palette, shown up with the identity.
function kinStrip(a) {
  const kin = el('div', 'kin');
  const row = (cls, k, v, help) => {
    const r = el('div', 'krow ' + cls);
    const kk = el('span', 'k', k);
    if (STAT_HELP[help]) kk.title = STAT_HELP[help];
    r.append(kk, el('span', 'v', v));
    return r;
  };
  kin.append(
    row('rng', 'range', a.range_km != null ? (a.range_km / 1.852).toFixed(2) + ' nm' : '–', 'range'),
    row('alt', 'alt', a.alt != null ? Math.round(a.alt).toLocaleString() + ' ft' : '–', 'alt'),
    row('spd', 'spd', a.speed_kt != null ? Math.round(a.speed_kt) + ' kt' : '–', 'spd'),
    row('trk', 'trk', a.track != null ? Math.round(a.track) + '°' : '–', 'trk'));
  return kin;
}

// Fit diagnostics strip below the plot (how good the Doppler measurement is).
function renderStats(box, a) {
  const f2 = v => (v == null ? '–' : Number(v).toFixed(2));
  const fit = el('div', 'fit');
  fit.append(
    _statChip('', 'corr ' + f2(a.corr), 'corr'),
    _statChip('', 'conf ' + f2(a.conf), 'conf'),
    _statChip('', a.bursts + ' brst', 'bursts'),
    _statChip('', a.rssi != null ? Math.round(a.rssi) + ' dBFS' : '–', 'sig'));
  box.replaceChildren(fit);
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
// SDR status light in the header. green = frames decoding, amber = dongle
// connected but nothing decoded lately (quiet sky / no antenna), red = capture
// died or the server is unreachable. `s` is null when the SDR state is unknown.
function updateSdr(s) {
  const el = document.getElementById('h-sdr');
  if (!el) return;
  let cls = 'down', txt = 'SDR offline';
  if (s) {
    if (s.state === 'receiving') { cls = 'ok'; txt = 'receiving'; }
    else if (s.state === 'idle') { cls = 'idle'; txt = 'no ADS-B'; }
    else { cls = 'down'; txt = s.error ? 'SDR error' : 'SDR offline'; }
  }
  el.className = 'stat sdr ' + cls;
  document.getElementById('h-sdr-txt').textContent = txt;
}

// All-time records rotate one-at-a-time through this list (every few seconds),
// each showing the value plus the flight/tail, type, and time that set it.
// Descent stays signed (negative), so it reads opposite to climb at a glance.
const RECORD_SPECS = [
  { key: 'speed_kt',      label: 'fastest',          unit: ' kt',  d: 0, cls: 'spd' },
  { key: 'speed_min_kt',  label: 'slowest',          unit: ' kt',  d: 0, cls: 'spd' },
  { key: 'alt_ft',        label: 'highest',          unit: ' ft',  d: 0, cls: 'alt' },
  { key: 'alt_min_ft',    label: 'lowest',           unit: ' ft',  d: 0, cls: 'alt' },
  { key: 'vrate_max_fpm', label: 'climb',            unit: ' fpm', d: 0, cls: 'vrt' },
  { key: 'vrate_min_fpm', label: 'descent',          unit: ' fpm', d: 0, cls: 'vrt' },
  { key: 'range_nm',      label: 'farthest',         unit: ' nm',  d: 2, cls: 'rng' },
  { key: 'closest_nm',    label: 'nearest',          unit: ' nm',  d: 2, cls: 'rng' },
  { key: 'sig_max_db',    label: 'strongest',        unit: ' dBFS', d: 1, cls: 'sig' },
  { key: 'sig_min_db',    label: 'weakest',          unit: ' dBFS', d: 1, cls: 'sig' },
  { key: 'dop_span_hz',   label: 'widest Δf',        unit: ' Hz',  d: 0, cls: 'dop' },
];
let recordIdx = -1;   // index of the record currently shown; -1 = none yet
let recordHold = 0;   // epoch(ms) until which auto-rotate is paused (after a tap)
let recordHover = false;

// Human "time since" for all-time records (which may be days old).
function relTime(ts) {
  const s = Math.max(0, (serverNow || (Date.now() / 1000)) - ts);
  if (s < 45) return 'just now';
  if (s < 5400) return Math.max(1, Math.round(s / 60)) + ' min ago';
  if (s < 172800) return Math.round(s / 3600) + ' hr ago';
  return Math.round(s / 86400) + ' d ago';
}

function renderRecord(el, spec, r) {
  el.className = 'record ' + spec.cls;
  el.querySelector('.rec-label').textContent = spec.label;         // name
  el.querySelector('.rec-val').textContent =                       // value
    r.value.toLocaleString(undefined, { maximumFractionDigits: spec.d }) + spec.unit;
  el.querySelector('.rec-date').textContent = r.ts ? relTime(r.ts) : '';   // when
  const who = [r.flight || r.reg,          // callsign if known, else the tail
               [r.make, r.model].filter(Boolean).join(' ')].filter(Boolean).join(' · ');
  el.querySelector('.rec-who').textContent = who || r.icao || '';
}

// The record object currently shown in the ticker (has ts + icao), or null.
function currentRecord() {
  return recordIdx >= 0 ? (latest.records || {})[RECORD_SPECS[recordIdx].key] : null;
}

// Enter a same-window replay of the session that set the record at `ts`, with
// `icao` selected at that instant. No covering session -> leave a brief hint.
async function replayRecord(ts, icao) {
  let hit;
  try { hit = await (await fetch('/api/session?at=' + ts)).json(); }
  catch (e) { return; }
  if (!hit || !hit.session) return;          // unrecorded / deleted session: no-op
  viewSession = hit.session;
  playing = false;
  await pollTimeline();                       // load that session's [start, end]
  seek(ts);                                   // fetchState(ts) with the session set
  if (icao) { selected = icao;
    document.getElementById('rail').classList.add('selected');
    pendingFly = icao;                        // fly in close once the frame renders
  }
  showReplayBanner(ts);
}

// Compact, never-truncated banner: "⧗ replaying · 3 d ago", with the exact
// moment on hover. The scrubber clock carries the live, updating time-ago.
function showReplayBanner(ts) {
  const b = document.getElementById('replay-banner');
  b.querySelector('.rb-txt').textContent = '⧗ replaying · ' + relTime(ts);
  b.title = 'record set ' + new Date(ts * 1000).toLocaleString();
  b.hidden = false;
}

// The record is advanced by the ⟳ cycle button and by the slow auto-rotate.
// Show the record at index i with a fade, tracking it as the current one.
function showRecordAt(i) {
  const el = document.getElementById('h-record');
  if (!el) return;
  recordIdx = i;
  const spec = RECORD_SPECS[i], r = (latest.records || {})[spec.key];
  el.style.opacity = 0;
  setTimeout(() => { renderRecord(el, spec, r); el.style.opacity = 1; }, 200);
}

// Step to the next (dir=+1) or previous (dir=-1) record that has data.
function stepRecord(dir) {
  const N = RECORD_SPECS.length, recs = latest.records || {};
  for (let n = 1; n <= N; n++) {
    const i = ((recordIdx + dir * n) % N + N) % N;
    const r = recs[RECORD_SPECS[i].key];
    if (r && r.value != null) { showRecordAt(i); return; }
  }
}

// Called on every state update: keep the shown record's value current, and show
// the first available record once any exist (cycle button / auto-rotate navigate).
function refreshRecord() {
  const el = document.getElementById('h-record');
  if (!el) return;
  const recs = latest.records || {};
  if (recordIdx >= 0) {
    const spec = RECORD_SPECS[recordIdx], r = recs[spec.key];
    if (r && r.value != null) { renderRecord(el, spec, r); return; }
    recordIdx = -1;                        // current record lost its data
  }
  for (let n = 0; n < RECORD_SPECS.length; n++) {
    const r = recs[RECORD_SPECS[n].key];
    if (r && r.value != null) {
      recordIdx = n; renderRecord(el, RECORD_SPECS[n], r); return;
    }
  }
  el.className = 'record';                  // nothing has data yet
  el.querySelector('.rec-label').textContent = 'records';
  el.querySelector('.rec-val').textContent = '–';
  el.querySelector('.rec-date').textContent = '';
  el.querySelector('.rec-who').textContent = 'waiting for aircraft…';
}

// Great-circle point `distNm` from (lat,lon) along bearing `brgDeg`.
function destPoint(lat, lon, brgDeg, distNm) {
  const R = 3440.065;                       // Earth radius, nm
  const d = distNm / R, brg = brgDeg * Math.PI / 180;
  const p1 = lat * Math.PI / 180, l1 = lon * Math.PI / 180;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) +
                       Math.cos(p1) * Math.sin(d) * Math.cos(brg));
  const l2 = l1 + Math.atan2(Math.sin(brg) * Math.sin(d) * Math.cos(p1),
                             Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [p2 * 180 / Math.PI, l2 * 180 / Math.PI];
}

// Redraw the coverage polygon (per-bearing farthest range) when the layer is on.
let coveragePoly = null;
function updateCoverage(cov) {
  if (!map.hasLayer(coverageLayer) || !cov || !receiverMarker) return;
  const rx = receiverMarker.getLatLng(), step = cov.step_deg;
  const pts = cov.sectors.map((r, i) =>
    destPoint(rx.lat, rx.lng, i * step + step / 2, r || 0));   // r=0 pulls to centre
  if (coveragePoly) coverageLayer.removeLayer(coveragePoly);
  coveragePoly = L.polygon(pts, { color: '#4be3e9', weight: 1.5, opacity: 0.75,
    fillColor: '#4be3e9', fillOpacity: 0.08, lineJoin: 'round', interactive: false });
  coverageLayer.addLayer(coveragePoly);
}

// Receiver oscillator estimate in the header. Offset is noisy (it averages out
// transmitter offsets over many aircraft), so smooth it; drift is steadier.
// Header oscillator readout: the server accumulates a robust absolute offset
// (median over many aircraft, invariant to --ppm), so we just display it. It
// stays put once we've ever had an estimate; dims when not actively refreshing.
let clockPpm = null;
function updateClock(c) {
  const el = document.getElementById('h-clock');
  if (!el) return;
  if (!c) { if (clockPpm == null) el.style.display = 'none'; return; }
  el.style.display = '';
  clockPpm = c.offset_ppm;                       // absolute crystal offset (ppm)
  el.classList.toggle('stale', !c.fresh_n);      // dim while not fitting right now
  el.querySelector('.v').textContent = (clockPpm >= 0 ? '+' : '') + clockPpm.toFixed(1);
  const cur = c.ppm || 0, residual = clockPpm - cur, drift = c.drift_ppm_min;
  el.title =
    `Estimated receiver oscillator offset ${clockPpm.toFixed(2)} ppm — median over `
    + `${c.n_aircraft} aircraft (last ${c.window_h} h). Suggested: --ppm ${Math.round(clockPpm)} `
    + `(currently --ppm ${cur}; residual ${residual >= 0 ? '+' : ''}${residual.toFixed(1)} ppm). `
    + (drift != null ? `Drift ${drift >= 0 ? '+' : ''}${drift.toFixed(2)} ppm/min. ` : '')
    + `Verify by applying it — the residual should head toward 0.`;
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
    : `${utc} · ${relTime(viewTime)}`;   // real time-ago, right of the PAST badge
  updateSdr(latest.sdr);
  updateClock(latest.clock);
  updateCoverage(latest.coverage);
  // uptime from the server's stable capture-start clock; before any data is
  // recorded the timeline's start is just "now" and would flicker, so prefer
  // the server value and only fall back to the timeline when it's absent.
  const up = latest.uptime != null ? latest.uptime
           : ((tl && tl.start) ? serverNow - tl.start : 0);
  document.getElementById('h-uptime').innerHTML = `${dur(up)}<span class="u">up</span>`;
  // decode rate as bursts/min over a fixed recent window (a sparse dipole feed
  // reads ~0 on a per-second rate; per-minute is meaningful).
  let perMin = 0;
  if (tl && tl.buckets && tl.buckets.length) {
    const bw = (tl.end - tl.start) / tl.buckets.length || 1;   // seconds/bucket
    const win = Math.min(60, tl.end - tl.start) || 1;          // seconds
    const nb = Math.max(1, Math.round(win / bw));
    const recent = tl.buckets.slice(-nb).reduce((a, b) => a + b, 0);
    perMin = recent * 60 / (nb * bw);
  }
  document.getElementById('h-rate').innerHTML =
    `${perMin < 10 ? perMin.toFixed(1) : perMin.toFixed(0)}<span class="u">burst/min</span>`;
  // Only flag PAST while scrubbing; when live the highlighted LIVE button is
  // the sole indicator (no duplicated "LIVE").
  const badge = document.getElementById('h-mode');
  badge.style.display = mode === 'live' ? 'none' : '';
  badge.textContent = isReplay ? 'REPLAY' : 'PAST';
  badge.className = 'badge past';
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
  if (viewSession) {           // leaving a record replay: drop the session override
    viewSession = null;
    document.getElementById('replay-banner').hidden = true;
    // the selected plane and every marker belong to that past session - clear
    // them so the live frame starts clean (fetchState below repopulates live).
    selected = null;
    document.getElementById('rail').classList.remove('selected');
    for (const icao of Object.keys(layers)) removeEntry(icao);
    pollTimeline();            // restore the home (live / launch-replay) strip
  }
  if (isReplay) {              // no live frame in replay: resume the looping sweep
    mode = 'past'; playing = true;
    document.getElementById('console').classList.add('past');
    updateTransport(); positionPlayhead();
    return;
  }
  mode = 'live'; playing = false; speed = 1;   // returning to live resets speed
  document.getElementById('console').classList.remove('past');
  updateTransport(); positionPlayhead();
  fetchState(null);
}
// Replay: auto-play the recorded session from the start and loop forever. There
// is no true "live" frame, so the playhead just sweeps [t_start, t_end].
function startReplay() {
  isReplay = true;
  document.getElementById('live').style.display = 'none';  // no "live" in replay
  const s = tl.speed || 1;
  speed = SPEEDS.includes(s) ? s
        : SPEEDS.reduce((a, b) => Math.abs(b - s) < Math.abs(a - s) ? b : a);
  mode = 'past';
  viewTime = tl.t_start ?? tl.start;
  playing = true;
  document.getElementById('console').classList.add('past');
  updateTransport(); positionPlayhead(); fetchState(viewTime);
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
    const p = new URLSearchParams();
    if (at != null) p.set('at', at);
    if (viewSession) p.set('session', viewSession);   // reconstruct that session
    const q = p.toString();
    const r = await fetch('/api/state' + (q ? '?' + q : ''));
    render(await r.json());
  } catch (e) {
    // server unreachable: keep the last frame but flag the light red
    const el = document.getElementById('h-sdr');
    if (el) { el.className = 'stat sdr down';
      document.getElementById('h-sdr-txt').textContent = 'offline'; }
  } finally { inflight = false; }
}
async function pollTimeline() {
  try {
    const r = await fetch('/api/timeline' + (viewSession ? '?session=' + viewSession : ''));
    tl = await r.json();
    document.getElementById('transport').classList.toggle('norecord', !tl.recording);
    drawTimeline();
    if (tl.replay && !isReplay) startReplay();
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
// Delegated so a click still selects even though renderList rebuilds the rows
// every second (a per-row handler can be torn out from under the pointer).
// pointerdown (not click): fires on press alone, so a selection can't be
// swallowed when the 1s list rebuild lands between mousedown and mouseup.
document.getElementById('list').addEventListener('pointerdown', e => {
  const hit = e.target.closest('[data-icao]');   // full card OR collapsed pill
  if (hit) select(hit.dataset.icao);
});
// mobile: the detail is a drawer; the close handle deselects to dismiss it
document.getElementById('sheet-close').addEventListener('click', () => {
  if (selected) select(selected);
});
// mobile: swipe the drawer down to dismiss it - the sheet follows the finger and
// then snaps closed (past a threshold) or springs back, using the CSS transform
// + transition already on #detail (no library, no scroll-snap gymnastics).
(() => {
  const sheet = document.getElementById('detail');
  let y0 = null, atTop = false, dragging = false;
  sheet.addEventListener('touchstart', e => {
    if (window.innerWidth > 760) { y0 = null; return; }
    y0 = e.touches[0].clientY; atTop = sheet.scrollTop <= 0; dragging = false;
  }, { passive: true });
  sheet.addEventListener('touchmove', e => {
    if (y0 == null || !atTop) return;
    const dy = e.touches[0].clientY - y0;
    if (dy > 0) {                          // dragging down from the top -> follow
      dragging = true;
      sheet.style.transition = 'none';
      sheet.style.transform = `translateY(${dy}px)`;
    }
  }, { passive: true });
  sheet.addEventListener('touchend', e => {
    if (y0 == null) return;
    const dy = e.changedTouches[0].clientY - y0;
    sheet.style.transition = '';           // restore CSS transition for the snap
    sheet.style.transform = '';            // hand back to CSS (translateY 0, or 100% on close)
    if (dragging && dy > 90 && selected) select(selected);   // far enough -> close
    y0 = null; dragging = false;
  }, { passive: true });
})();

// ---- photo lightbox (mobile only: tap the small thumbnail to see it larger) --
function openLightbox(full, link, by) {
  document.getElementById('lb-img').src = full;
  const cap = document.getElementById('lb-credit');
  cap.replaceChildren();
  if (link) {
    const a = document.createElement('a');
    a.href = link; a.target = '_blank'; a.rel = 'noopener';
    a.textContent = by ? '© ' + by : 'view source';    // untrusted -> textContent
    cap.appendChild(a);
  } else if (by) {
    cap.textContent = '© ' + by;
  }
  document.getElementById('lightbox').hidden = false;
}
function closeLightbox() {
  document.getElementById('lightbox').hidden = true;
  document.getElementById('lb-img').src = '';
}
document.getElementById('detail').addEventListener('click', e => {
  const img = e.target.closest('.photo img');
  if (img) {
    const fig = img.closest('.photo');
    openLightbox(fig.dataset.full, fig.dataset.link, fig.dataset.by);
  }
});
document.getElementById('lightbox').addEventListener('click', e => {
  if (e.target.id === 'lightbox' || e.target.closest('#lb-close')) closeLightbox();
});

// playback tick: advance the virtual clock, snap back to live at the end
setInterval(() => {
  if (!playing || mode !== 'past' || !tl) return;
  let next = viewTime + speed * 0.25;
  if (next >= tl.end) {
    // record replay: stop at the session end, stay in the session (LIVE exits)
    if (viewSession) { viewTime = tl.end; playing = false;
      updateTransport(); positionPlayhead(); fetchState(viewTime); return; }
    if (!isReplay) { playing = false; goLive(); return; }
    next = tl.t_start ?? tl.start;   // replay: loop back to the start
  }
  viewTime = next;
  updateTransport(); positionPlayhead(); fetchState(viewTime);
}, 250);

// ---- keyboard ------------------------------------------------------------
window.addEventListener('keydown', e => {
  if (e.key === ' ') { e.preventDefault(); document.getElementById('playPause').click(); }
  else if (e.key.toLowerCase() === 'l') goLive();
  else if (e.key === 'Escape') {
    if (!document.getElementById('lightbox').hidden) closeLightbox();
    else select(selected);
  }
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
buildLogo();
fetchState(null);
pollTimeline();
setInterval(() => { if (mode === 'live') fetchState(null); }, 1000);
setInterval(pollTimeline, 2000);
refreshRecord();
// Records slowly auto-advance (12s) so the panel stays alive and hints there
// are several to see - but pause while hovering, or for 30s after a manual tap,
// so rotation never pulls the record out from under you.
const _recEl = document.getElementById('h-record');
_recEl?.addEventListener('mouseenter', () => { recordHover = true; });
_recEl?.addEventListener('mouseleave', () => { recordHover = false; });
const _holdRecords = () => { recordHold = Date.now() + 30000; };
document.getElementById('rec-cycle')?.addEventListener('click', () => { stepRecord(1); _holdRecords(); });
document.getElementById('rec-replay')?.addEventListener('click', () => {
  const r = currentRecord();
  if (r && r.ts != null) replayRecord(r.ts, r.icao);
  _holdRecords();
});
document.getElementById('rb-live')?.addEventListener('click', goLive);
setInterval(() => {
  if (!recordHover && Date.now() >= recordHold && recordIdx >= 0) stepRecord(1);
}, 12000);
window.addEventListener('resize', () => { drawPlot(); drawTimeline(); });
