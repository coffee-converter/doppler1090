const map = L.map('map').setView([42.19, -88.19], 9);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  { maxZoom: 19, attribution: '© OpenStreetMap' }).addTo(map);

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
      { radius: 7, color: '#ffd400', fillColor: '#ffd400', fillOpacity: 1 })
      .addTo(map).bindTooltip('receiver');
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
      if (!entry.marker) {
        entry.marker = L.circleMarker([a.lat, a.lon])
          .addTo(map).on('click', () => select(a.icao));
      } else { entry.marker.setLatLng([a.lat, a.lon]); }
      entry.marker.bindTooltip(label, { permanent: false });
      styleMarker(entry.marker, isSel);
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

// Selected aircraft marker is gold and enlarged; others are small and blue.
function styleMarker(m, sel) {
  m.setStyle(sel
    ? { radius: 9, color: '#ffd400', weight: 3, fillColor: '#ffd400', fillOpacity: 0.95 }
    : { radius: 5, color: '#7fb0ff', weight: 1, fillColor: '#7fb0ff', fillOpacity: 0.7 });
  if (sel) m.bringToFront();
}

function select(icao) { selected = icao; render(latest); }  // instant re-highlight

function drawPlot() {
  const c = document.getElementById('plot');
  const ctx = c.getContext('2d');
  ctx.clearRect(0, 0, c.width, c.height);
  const a = latest.aircraft.find(x => x.icao === selected);
  const meta = document.getElementById('meta');
  if (!a) { meta.textContent = 'select an aircraft'; return; }
  const pts = a.doppler;
  const ts = pts.map(p => p.t), ms = pts.map(p => p.measured), ps = pts.map(p => p.predicted);
  const tmin = Math.min(...ts), tmax = Math.max(...ts);
  const ally = ms.concat(ps);
  const ymin = Math.min(...ally), ymax = Math.max(...ally);
  const pad = 30, W = c.width, H = c.height;
  const sx = t => pad + (W - 2*pad) * (tmax === tmin ? 0.5 : (t - tmin)/(tmax - tmin));
  const sy = y => H - pad - (H - 2*pad) * (ymax === ymin ? 0.5 : (y - ymin)/(ymax - ymin));
  // zero line
  ctx.strokeStyle = '#445'; ctx.beginPath();
  ctx.moveTo(pad, sy(0)); ctx.lineTo(W - pad, sy(0)); ctx.stroke();
  // predicted curve
  ctx.strokeStyle = '#5bd75b'; ctx.beginPath();
  pts.forEach((p, i) => { const x = sx(p.t), y = sy(p.predicted);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.stroke();
  // measured points
  ctx.fillStyle = '#7fb0ff';
  pts.forEach(p => { ctx.beginPath(); ctx.arc(sx(p.t), sy(p.measured), 2, 0, 7); ctx.fill(); });
  meta.textContent =
    `${a.flight || a.icao}\nscale ${a.scale}  corr ${a.corr}  conf ${a.conf}\n` +
    `bursts ${a.bursts}  range ${a.range_km} km\n` +
    `alt ${a.alt ?? '-'} ft  spd ${a.speed_kt ?? '-'} kt  trk ${a.track ?? '-'}°`;
}

async function poll() {
  try {
    const r = await fetch('/api/state');
    render(await r.json());
  } catch (e) { /* keep last frame on transient errors */ }
}
poll();
setInterval(poll, 1000);
