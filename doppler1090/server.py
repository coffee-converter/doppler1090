import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np
from .geometry import geodetic_to_ecef
from .terminal import confidence


def build_snapshot(store, rx_llh, min_conf=0.0, at=None, server_time=None,
                   type_store=None):
    """Build the JSON-serializable dashboard state. Caller holds store.lock.

    ``at`` is the epoch the frame represents (None => live). ``server_time`` is
    the current epoch, so the client can tell how far behind live it is.
    ``type_store`` (optional) supplies cached aircraft make/model/registration."""
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
        # make/model/registration + a photo from the cache (None until resolved;
        # calling get()/get_photo() schedules a background lookup on first sight)
        info = type_store.get(icao) if type_store is not None else None
        reg = (info or {}).get("reg")
        photo = (type_store.get_photo(reg)
                 if (type_store is not None and reg) else None)
        aircraft.append({
            "icao": icao,
            "flight": s.get("flight"),
            "make": (info or {}).get("make"), "model": (info or {}).get("model"),
            "reg": reg, "type": (info or {}).get("type"),
            "photo": (photo or {}).get("url"),
            "photo_link": (photo or {}).get("link"),
            "photo_by": (photo or {}).get("by"),
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
        "at": _f(at),
        "server_time": float(server_time if server_time is not None else time.time()),
    }


def _f(v):
    return None if v is None else float(v)


WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
_CTYPES = {"html": "text/html", "js": "application/javascript",
           "css": "text/css", "json": "application/json",
           "png": "image/png", "jpg": "image/jpeg", "gif": "image/gif",
           "svg": "image/svg+xml", "ico": "image/x-icon"}


def _make_handler(store, rx_llh, lock, min_conf, history, type_store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # silence per-request logging

        def do_GET(self):
            route = urlparse(self.path)
            if route.path == "/api/state":
                self._state(parse_qs(route.query))
            elif route.path == "/api/timeline":
                self._timeline()
            else:
                self._static()

        def _state(self, query):
            at = query.get("at", [None])[0]
            # A past frame is reconstructed off-lock from the recorded file;
            # only the live frame touches the shared store.
            if at is not None and history is not None:
                at = float(at)
                past = history.reconstruct(at)
                body = json.dumps(build_snapshot(past, rx_llh, min_conf,
                                                 at=at, type_store=type_store)).encode()
            else:
                with lock:
                    body = json.dumps(build_snapshot(store, rx_llh, min_conf,
                                                     type_store=type_store)).encode()
            self._send(200, "application/json", body)

        def _timeline(self):
            if history is None:
                data = {"start": None, "end": None, "buckets": [],
                        "recording": False}
            else:
                data = history.timeline()
                data["recording"] = True
            self._send(200, "application/json", json.dumps(data).encode())

        def _static(self):
            clean = self.path.split("?", 1)[0]
            rel = "index.html" if clean in ("/", "") else clean.lstrip("/")
            full = os.path.normpath(os.path.join(WEB_DIR, rel))
            if not full.startswith(WEB_DIR + os.sep) or not os.path.isfile(full):
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


def make_server(store, rx_llh, lock, min_conf, port=0, history=None,
                type_store=None):
    return ThreadingHTTPServer(("127.0.0.1", port),
                               _make_handler(store, rx_llh, lock, min_conf,
                                             history, type_store))


def serve(store, rx_llh, lock, min_conf, port, open_browser=False, history=None,
          type_store=None):
    httpd = make_server(store, rx_llh, lock, min_conf, port, history=history,
                        type_store=type_store)
    actual = httpd.server_address[1]
    if open_browser:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{actual}/")
    print(f"doppler1090 web dashboard: http://127.0.0.1:{actual}/")
    httpd.serve_forever()
