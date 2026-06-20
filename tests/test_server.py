import json
import threading
import urllib.request
import numpy as np
from doppler1090.track import TrackStore
from doppler1090.server import build_snapshot, make_server


def _seed_tracking(store, icao, n=40, span=600.0, noise=10.0, seed=0,
                   flight=None, lat0=41.8, alt=10000.0, speed_kt=450.0):
    rng = np.random.default_rng(seed)
    if flight:
        store.update_callsign(icao, flight)
    for i in range(n):
        d = 0.5 * span * np.cos(np.pi * i / (n - 1))  # sweeps +span/2..-span/2
        lat = lat0 + 0.001 * i
        lon = -88.0 + 0.001 * i
        store.update_position(icao, float(i), lat, lon, alt)
        store.update_velocity(icao, float(i), speed_kt, 270.0, 0.0)
        f = 5000.0 + d + rng.normal(0, noise)
        store._inject(icao, float(i), f, d, lat, lon, 270.0)


def test_snapshot_has_receiver_and_aircraft():
    store = TrackStore()
    _seed_tracking(store, "AAA111", flight="UAL1", seed=1)
    _seed_tracking(store, "BBB222", flight="DAL2", seed=2)
    snap = build_snapshot(store, (42.0, -88.0, 240.0), min_conf=0.0)
    assert snap["receiver"] == {"lat": 42.0, "lon": -88.0, "alt": 240.0}
    icaos = [a["icao"] for a in snap["aircraft"]]
    assert "AAA111" in icaos and "BBB222" in icaos
    a = next(a for a in snap["aircraft"] if a["icao"] == "AAA111")
    assert a["flight"] == "UAL1"
    assert a["bursts"] == 40
    assert a["alt"] == 10000.0
    assert a["speed_kt"] == 450.0


def test_snapshot_ground_track_and_doppler_series():
    store = TrackStore()
    _seed_tracking(store, "AAA111", seed=1)
    _seed_tracking(store, "BBB222", seed=2)
    snap = build_snapshot(store, (42.0, -88.0, 240.0))
    a = next(a for a in snap["aircraft"] if a["icao"] == "AAA111")
    assert len(a["ground_track"]) == 40
    assert len(a["ground_track"][0]) == 3          # [lat, lon, predicted_doppler]
    assert len(a["doppler"]) == 40
    assert a["doppler"][0]["t"] == 0.0             # series time starts at 0
    assert {"t", "measured", "predicted"} <= set(a["doppler"][0])


def test_snapshot_filters_by_confidence():
    store = TrackStore()
    _seed_tracking(store, "GOOD11", span=600.0, noise=10.0, seed=1)
    _seed_tracking(store, "GOOD22", span=600.0, noise=10.0, seed=2)
    _seed_tracking(store, "FLAT33", span=20.0, noise=200.0, seed=3)
    icaos = [a["icao"] for a in build_snapshot(store, (42.0, -88.0, 240.0),
                                               min_conf=0.25)["aircraft"]]
    assert "GOOD11" in icaos
    assert "FLAT33" not in icaos


def test_snapshot_is_json_serializable():
    store = TrackStore()
    _seed_tracking(store, "AAA111", seed=1)
    _seed_tracking(store, "BBB222", seed=2)
    snap = build_snapshot(store, (42.0, -88.0, 240.0))
    # round-trips with no custom encoder (no numpy types leak through)
    rt = json.loads(json.dumps(snap))
    assert rt["receiver"]["lat"] == 42.0
    assert isinstance(rt["aircraft"][0]["doppler"][0]["measured"], float)


def test_http_endpoints_serve_state_and_index():
    store = TrackStore()
    _seed_tracking(store, "AAA111", seed=1)
    _seed_tracking(store, "BBB222", seed=2)
    lock = store.lock
    httpd = make_server(store, (42.0, -88.0, 240.0), lock, 0.0, port=0)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state") as r:
            assert r.status == 200
            data = json.loads(r.read())
        assert "receiver" in data and "aircraft" in data
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
            assert r.status == 200
            assert b"<html" in r.read().lower()
    finally:
        httpd.shutdown()


def test_static_strips_query_string():
    store = TrackStore()
    httpd = make_server(store, (42.0, -88.0, 240.0), store.lock, 0.0, port=0)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html?v=2") as r:
            assert r.status == 200
    finally:
        httpd.shutdown()
