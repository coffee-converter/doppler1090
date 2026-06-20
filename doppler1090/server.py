import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from .geometry import geodetic_to_ecef
from .terminal import confidence


def build_snapshot(store, rx_llh, min_conf=0.0):
    """Build the JSON-serializable dashboard state. Caller holds store.lock."""
    rx = geodetic_to_ecef(*rx_llh)
    fits = store.joint_fit()
    aircraft = []
    for icao in store.icaos():
        fit = fits.get(icao)
        if fit is None:
            continue
        conf = confidence(fit)
        if conf < min_conf:
            continue
        s = store.latest(icao)
        samples = store._samples[icao]
        rng_km = 0.0
        if all(k in s for k in ("lat", "lon", "alt")):
            ac = geodetic_to_ecef(s["lat"], s["lon"], s["alt"])
            rng_km = float(np.linalg.norm(ac - rx) / 1000.0)
        t0 = samples[0].t
        measured = fit.measured_doppler
        ground_track = [[float(x.lat), float(x.lon), float(x.doppler_pred)]
                        for x in samples]
        doppler = [{"t": float(x.t - t0),
                    "measured": float(measured[i]),
                    "predicted": float(x.doppler_pred)}
                   for i, x in enumerate(samples)]
        aircraft.append({
            "icao": icao,
            "flight": s.get("flight"),
            "lat": _f(s.get("lat")), "lon": _f(s.get("lon")), "alt": _f(s.get("alt")),
            "speed_kt": _f(s.get("speed_kt")), "track": _f(s.get("track")),
            "vrate": _f(s.get("vrate_fpm")),
            "range_km": round(rng_km, 1),
            "scale": round(float(fit.scale), 2),
            "corr": round(float(fit.correlation), 2),
            "conf": round(float(conf), 2),
            "quality": round(float(fit.quality), 2),
            "bursts": int(fit.n),
            "ground_track": ground_track,
            "doppler": doppler,
        })
    return {
        "receiver": {"lat": _f(rx_llh[0]), "lon": _f(rx_llh[1]), "alt": _f(rx_llh[2])},
        "aircraft": aircraft,
    }


def _f(v):
    return None if v is None else float(v)


WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
_CTYPES = {"html": "text/html", "js": "application/javascript",
           "css": "text/css", "json": "application/json"}


def _make_handler(store, rx_llh, lock, min_conf):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # silence per-request logging

        def do_GET(self):
            if self.path.split("?", 1)[0] == "/api/state":
                with lock:
                    body = json.dumps(build_snapshot(store, rx_llh, min_conf)).encode()
                self._send(200, "application/json", body)
            else:
                self._static()

        def _static(self):
            rel = "index.html" if self.path in ("/", "") else self.path.lstrip("/")
            full = os.path.normpath(os.path.join(WEB_DIR, rel))
            if not full.startswith(WEB_DIR) or not os.path.isfile(full):
                self._send(404, "text/plain", b"not found")
                return
            ext = full.rsplit(".", 1)[-1]
            with open(full, "rb") as fh:
                body = fh.read()
            self._send(200, _CTYPES.get(ext, "application/octet-stream"), body)

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def make_server(store, rx_llh, lock, min_conf, port=0):
    return ThreadingHTTPServer(("127.0.0.1", port),
                               _make_handler(store, rx_llh, lock, min_conf))


def serve(store, rx_llh, lock, min_conf, port, open_browser=False):
    httpd = make_server(store, rx_llh, lock, min_conf, port)
    actual = httpd.server_address[1]
    if open_browser:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{actual}/")
    print(f"doppler1090 web dashboard: http://127.0.0.1:{actual}/")
    httpd.serve_forever()
