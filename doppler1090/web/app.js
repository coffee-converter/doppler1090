// Zoom locked to the range the FAA sectional cache actually covers (native
// tiles exist z8-z12; outside that the service 404s and tiles vanish). Max is
// 11 because detectRetina pulls one level deeper, so map-zoom 11 already shows
// the real z12 tiles.
const map = L.map('map', { minZoom: 7, maxZoom: 11 })
  .setView([41.88, -87.63], 10);   // Chicago; recenters on the receiver once state loads
const osm = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  { maxZoom: 19, detectRetina: true, attribution: '© OpenStreetMap' });
// FAA aeronautical charts — the same public-domain raster charts SkyVector
// uses, pre-tiled in Web Mercator and served from the FAA's own ArcGIS Online.
// {z}/{y}/{x} (ArcGIS tile order). US coverage only; blank outside CONUS/AK/HI.
// detectRetina pulls the next zoom level on HiDPI screens and packs it at 2x
// density — supersampling that keeps the fine chart linework crisp instead of
// aliased when the tiles would otherwise be scaled up.
const faaChart = name => L.tileLayer(
  `https://tiles.arcgis.com/tiles/ssFJjBXIUyZDrSYZ/arcgis/rest/services/${name}/MapServer/tile/{z}/{y}/{x}`,
  { maxZoom: 12, minNativeZoom: 8, maxNativeZoom: 12, opacity: 0.4,
    detectRetina: true, attribution: 'Aeronautical charts: FAA' });
const baseLayers = {
  'VFR Sectional': faaChart('VFR_Sectional'),
  'VFR Terminal': faaChart('VFR_Terminal'),
  'IFR Low': faaChart('IFR_AreaLow'),
  'IFR High': faaChart('IFR_High'),
  'OpenStreetMap': osm,
};
baseLayers['VFR Sectional'].addTo(map);
L.control.layers(baseLayers, {}, { position: 'topright' }).addTo(map);

let receiverMarker = null;
let layers = {};        // icao -> { marker, tracks: [polyline,...] }
let selected = null;
let latest = { aircraft: [] };

// Doppler (Hz) -> diverging color. + = approaching = blue, - = receding = red, 0 = white.
function dopColor(hz) {
  const x = Math.max(-1, Math.min(1, hz / 600));   // saturate at +/-600 Hz
  if (x >= 0) {                                     // white -> blue
    const t = x;
    return `rgb(${Math.round(255*(1-t))},${Math.round(255*(1-t))},255)`;
  }
  const t = -x;                                     // white -> red
  return `rgb(255,${Math.round(255*(1-t))},${Math.round(255*(1-t))})`;
}

function render(state) {
  latest = state;
  if (!receiverMarker) {
    receiverMarker = L.circleMarker([state.receiver.lat, state.receiver.lon],
      { radius: 7, color: '#000', weight: 2, fillColor: '#ffd400', fillOpacity: 1 })
      .addTo(map).bindTooltip('receiver');
    map.setView([state.receiver.lat, state.receiver.lon], map.getZoom());  // snap off the placeholder to the real receiver
  }
  const seen = new Set();
  for (const a of state.aircraft) {
    seen.add(a.icao);
    let entry = layers[a.icao];
    if (!entry) { entry = { marker: null, tracks: [] }; layers[a.icao] = entry; }
    // ground track: one colored segment per consecutive sample pair
    entry.tracks.forEach(l => map.removeLayer(l));
    entry.tracks = [];
    const isSel = a.icao === selected;
    const g = a.ground_track;
    for (let i = 1; i < g.length; i++) {
      const seg = L.polyline([[g[i-1][0], g[i-1][1]], [g[i][0], g[i][1]]],
        { color: dopColor(g[i][2]), weight: isSel ? 7 : 4, opacity: 0.9 });
      seg.addTo(map); entry.tracks.push(seg);
    }
    const label = (a.flight || a.icao);
    if (a.lat != null) {
      const icon = planeIcon(a.track, isSel);
      if (!entry.marker) {
        entry.marker = L.marker([a.lat, a.lon], { icon })
          .addTo(map).on('click', () => select(a.icao));
      } else {
        entry.marker.setLatLng([a.lat, a.lon]);
        entry.marker.setIcon(icon);
      }
      entry.marker.setZIndexOffset(isSel ? 1000 : 0);
      entry.marker.bindTooltip(label, { permanent: false });
    }
  }
  // drop aircraft no longer present
  for (const icao of Object.keys(layers)) {
    if (!seen.has(icao)) {
      const e = layers[icao];
      if (e.marker) map.removeLayer(e.marker);
      e.tracks.forEach(l => map.removeLayer(l));
      delete layers[icao];
    }
  }
  renderList();
  drawPlot();
}

function renderList() {
  const div = document.getElementById('list');
  div.innerHTML = '';
  for (const a of latest.aircraft) {
    const b = document.createElement('button');
    b.textContent = `${(a.flight || a.icao)}  conf ${a.conf}  scale ${a.scale}`;
    if (a.icao === selected) b.className = 'sel';
    b.onclick = () => select(a.icao);
    div.appendChild(b);
  }
}

// Rotated airplane icon (points along the ground track). White with a dark
// outline + drop shadow so it stands out over any map tile; gold and enlarged
// when selected.
function planeIcon(track, sel) {
  const size = sel ? 38 : 28;
  const fill = sel ? '#ffd400' : '#ffffff';
  const stroke = sel ? '#5a4500' : '#11151c';
  const rot = track || 0;  // ground track in degrees (0 = north)
  const svg =
    `<svg width="${size}" height="${size}" viewBox="0 0 24 24" ` +
    `style="transform:rotate(${rot}deg);filter:drop-shadow(0 0 2px rgba(0,0,0,0.9))">` +
    `<path d="M12 2 L13.4 9 L22 13.2 L22 15 L13.4 12.4 L12.9 19 L15.5 20.6 L15.5 22 ` +
    `L12 21 L8.5 22 L8.5 20.6 L11.1 19 L10.6 12.4 L2 15 L2 13.2 L10.6 9 Z" ` +
    `fill="${fill}" stroke="${stroke}" stroke-width="1"/></svg>`;
  return L.divIcon({ html: svg, className: 'plane-icon',
                     iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
}

function select(icao) { selected = icao; render(latest); }  // instant re-highlight

// "Nice" evenly-spaced ticks covering [min, max], with AT MOST `count` of them.
// The step snaps UP to the next 1/2/5 x 10^n so a wider range yields a coarser
// grid (fewer labels) rather than cramming more in. Returns {step, values} so
// the caller can pick label decimals from the step.
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

// Chart palette (dark surface #0c0f14)
const COL = {
  grid: '#1a2331', axis: '#38445a', zero: '#6b7c94', ink: '#8b97a8',
  measured: '#7fb0ff', predicted: '#54c07a',
};

function drawPlot() {
  const c = document.getElementById('plot');
  const ctx = c.getContext('2d');
  // Match the backing store to the CSS box * devicePixelRatio so text and
  // marks render crisp on HiDPI displays and fill the panel width.
  const dpr = window.devicePixelRatio || 1;
  const W = c.clientWidth || 380, H = c.clientHeight || 300;
  if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) {
    c.width = Math.round(W * dpr); c.height = Math.round(H * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const a = latest.aircraft.find(x => x.icao === selected);
  const meta = document.getElementById('meta');
  if (!a) {
    ctx.fillStyle = COL.ink; ctx.font = '13px system-ui, sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('select an aircraft', W / 2, H / 2);
    meta.replaceChildren(el('div', 'placeholder', 'select an aircraft')); return;
  }
  const pts = a.doppler;
  const ts = pts.map(p => p.t), ms = pts.map(p => p.measured), ps = pts.map(p => p.predicted);
  const tmin = Math.min(...ts), tmax = Math.max(...ts);
  const ally = ms.concat(ps, [0]);   // always include 0 so the zero line is in view
  const ymin = Math.min(...ally), ymax = Math.max(...ally);
  const padL = 54, padR = 14, padT = 16, padB = 34;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const sx = t => padL + plotW * (tmax === tmin ? 0.5 : (t - tmin) / (tmax - tmin));
  const sy = y => padT + plotH * (ymax === ymin ? 0.5 : 1 - (y - ymin) / (ymax - ymin));

  ctx.font = '11px system-ui, sans-serif';
  ctx.lineWidth = 1;
  // horizontal gridlines + y tick labels (tick count scales with plot height)
  const yTicks = niceTicks(ymin, ymax, Math.max(2, Math.floor(plotH / 44)));
  const yDec = yTicks.step < 1 ? 1 : 0;
  ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  for (const v of yTicks.values) {
    const y = Math.round(sy(v)) + 0.5;
    ctx.strokeStyle = COL.grid; ctx.beginPath();
    ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
    ctx.fillStyle = COL.ink; ctx.fillText(v.toFixed(yDec), padL - 8, sy(v));
  }
  // vertical gridlines + x tick labels (tick count scales with plot width, so
  // a long pass gets a coarser time grid instead of crowded labels)
  const xTicks = niceTicks(tmin, tmax, Math.max(2, Math.floor(plotW / 60)));
  const xDec = xTicks.step < 1 ? 1 : 0;
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  for (const v of xTicks.values) {
    const x = Math.round(sx(v)) + 0.5;
    ctx.strokeStyle = COL.grid; ctx.beginPath();
    ctx.moveTo(x, padT); ctx.lineTo(x, H - padB); ctx.stroke();
    ctx.fillStyle = COL.ink; ctx.fillText(v.toFixed(xDec), sx(v), H - padB + 8);
  }
  // zero line (the Doppler sign-flip / closest-approach reference — kept
  // clearly brighter and thicker than the grid)
  const yz = Math.round(sy(0)) + 0.5;
  ctx.strokeStyle = COL.zero; ctx.lineWidth = 1.5; ctx.beginPath();
  ctx.moveTo(padL, yz); ctx.lineTo(W - padR, yz); ctx.stroke();
  ctx.lineWidth = 1;

  // axis titles
  ctx.fillStyle = COL.ink; ctx.font = '11px system-ui, sans-serif';
  ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
  ctx.fillText('time (s)', padL + plotW / 2, H - 4);
  ctx.save(); ctx.translate(12, padT + plotH / 2); ctx.rotate(-Math.PI / 2);
  ctx.textBaseline = 'top'; ctx.fillText('Doppler (Hz)', 0, 0); ctx.restore();

  // predicted curve (2px, rounded)
  ctx.strokeStyle = COL.predicted; ctx.lineWidth = 2;
  ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.beginPath();
  pts.forEach((p, i) => { const x = sx(p.t), y = sy(p.predicted);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.stroke();
  ctx.lineWidth = 1;
  // measured points (surface-colored ring so overlapping dots separate)
  ctx.fillStyle = COL.measured; ctx.strokeStyle = '#0c0f14';
  pts.forEach(p => { ctx.beginPath(); ctx.arc(sx(p.t), sy(p.measured), 2.6, 0, 7);
    ctx.fill(); ctx.stroke(); });

  // legend (top-right, inside the plot)
  ctx.font = '11px system-ui, sans-serif'; ctx.textBaseline = 'middle';
  const items = [['measured', COL.measured, 'dot'], ['predicted', COL.predicted, 'line']];
  const lw = 78, lx = W - padR - lw, ly = padT + 6;
  items.forEach(([label, color, kind], i) => {
    const y = ly + i * 16;
    if (kind === 'dot') { ctx.fillStyle = color;
      ctx.beginPath(); ctx.arc(lx + 4, y, 2.6, 0, 7); ctx.fill(); }
    else { ctx.strokeStyle = color; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(lx, y); ctx.lineTo(lx + 9, y); ctx.stroke(); ctx.lineWidth = 1; }
    ctx.fillStyle = COL.ink; ctx.textAlign = 'left';
    ctx.fillText(label, lx + 14, y);
  });

  renderMeta(meta, a);
}

// Small DOM helper: create <tag class=cls> with optional text content.
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// Structured stat block below the chart (built with safe DOM APIs, not
// innerHTML — the callsign is attacker-influenceable ADS-B text).
function renderMeta(meta, a) {
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
  if (a.flight) frag.push(el('div', 'sub', a.icao));   // show hex id when a callsign exists
  frag.push(grid);
  meta.replaceChildren(...frag);
}

async function poll() {
  try {
    const r = await fetch('/api/state');
    render(await r.json());
  } catch (e) { /* keep last frame on transient errors */ }
}
poll();
window.addEventListener('resize', () => { if (latest.aircraft) drawPlot(); });
setInterval(poll, 1000);
