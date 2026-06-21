import numpy as np
from doppler1090.track import TrackStore
from doppler1090.geometry import radial_velocity, predicted_doppler


def _populate(store, icao, n, track_deg_fn, drift=0.7, const=1234.0, scale=1.0):
    rx = (0.0, 0.0, 0.0)
    for i in range(n):
        t = float(i)
        trk = track_deg_fn(i)
        store.update_position(icao, t, lat=0.0, lon=0.01 + 0.0001 * i, alt=10000.0)
        store.update_velocity(icao, t, speed_kt=480.0, track_deg=trk, vrate_fpm=0.0)
        # synth f_offset = const + drift*t + scale*predicted_doppler(+noise-free)
        latest = store.latest(icao)
        from doppler1090.geometry import radial_velocity, predicted_doppler
        vr = radial_velocity(rx, (latest["lat"], latest["lon"], latest["alt"]),
                             latest["speed"], latest["track"], latest["vrate"])
        dop = predicted_doppler(vr)
        f_off = const + drift * t + scale * dop
        store._inject(icao, t, f_off, dop, latest["lat"], latest["lon"], trk)


def test_fit_recovers_scale_and_correlation_for_straight_pass():
    store = TrackStore()
    _populate(store, "abc123", n=40, track_deg_fn=lambda i: 270.0)
    fit = store.fit("abc123")
    assert fit is not None
    assert abs(fit.scale - 1.0) < 0.05
    assert fit.correlation > 0.99
    assert fit.quality > 0.8


def test_turning_aircraft_has_low_quality():
    store = TrackStore()
    _populate(store, "def456", n=40, track_deg_fn=lambda i: (i * 9) % 360)
    assert store.quality("def456") < 0.5


def test_fit_none_when_too_few_bursts():
    store = TrackStore()
    _populate(store, "ghi789", n=4, track_deg_fn=lambda i: 270.0)
    assert store.fit("ghi789") is None


def test_fit_recovers_doppler_track_with_noise_on_realistic_pass():
    rng = np.random.default_rng(1)
    store = TrackStore()
    rx = (52.0, 4.0, 0.0)
    for i in range(40):
        t = float(i)
        lat = 51.85 + 0.0075 * i  # straight pass crossing near overhead
        store.update_position("ac", t, lat, 4.0, 10000.0)
        store.update_velocity("ac", t, 480.0, 0.0, 0.0)
        s = store.latest("ac")
        vr = radial_velocity(rx, (s["lat"], s["lon"], s["alt"]),
                             s["speed"], s["track"], s["vrate"])
        dop = predicted_doppler(vr)
        f = 1234.0 + 0.7 * t + 1.0 * dop + rng.normal(0, 50.0)
        store._inject("ac", t, f, dop, lat, 4.0, 0.0)
    fit = store.fit("ac")
    assert abs(fit.scale - 1.0) < 0.1
    assert fit.correlation > 0.9


def test_fit_rejects_offsets_that_do_not_track_doppler():
    # Same realistic pass, but the measured offsets carry NO Doppler component
    # (only baseline + noise). The fit must NOT report a false Doppler track.
    rng = np.random.default_rng(2)
    store = TrackStore()
    rx = (52.0, 4.0, 0.0)
    for i in range(40):
        t = float(i)
        lat = 51.85 + 0.0075 * i
        store.update_position("ac", t, lat, 4.0, 10000.0)
        store.update_velocity("ac", t, 480.0, 0.0, 0.0)
        s = store.latest("ac")
        vr = radial_velocity(rx, (s["lat"], s["lon"], s["alt"]),
                             s["speed"], s["track"], s["vrate"])
        dop = predicted_doppler(vr)
        f = 1234.0 + 0.7 * t + rng.normal(0, 50.0)
        store._inject("ac", t, f, dop, lat, 4.0, 0.0)
    fit = store.fit("ac")
    assert abs(fit.scale) < 0.2
    assert abs(fit.correlation) < 0.5


def test_update_callsign_appears_in_latest():
    store = TrackStore()
    store.update_callsign("abc123", "UAL456")
    assert store.latest("abc123")["flight"] == "UAL456"


def test_joint_fit_removes_shared_drift_and_recovers_unit_scale():
    # Three aircraft with DIFFERENT (linear) Doppler, plus a SHARED receiver
    # clock drift. A drift-ignoring per-aircraft fit inflates Scale; the joint
    # fit (which models the shared drift) recovers Scale ~ 1.
    import numpy as np
    store = TrackStore()
    n = 40
    tn = np.linspace(0, 1, n)
    drift = 400.0 * tn  # shared receiver clock drift (Hz)
    planes = {
        "A": -100.0 + 300.0 * tn,
        "B": 50.0 - 250.0 * tn,
        "C": 200.0 * tn,
    }
    consts = {"A": 1000.0, "B": -500.0, "C": 3000.0}
    for ic, d in planes.items():
        f = consts[ic] + 1.0 * d + drift  # true scale = 1
        for i in range(n):
            store._inject(ic, float(i), float(f[i]), float(d[i]), 0.0, 0.0, 270.0)

    fits = store.joint_fit(drift_order=1)
    for ic in planes:
        assert abs(fits[ic].scale - 1.0) < 0.1, (ic, fits[ic].scale)
        assert fits[ic].correlation > 0.99

    # Naive drift-ignoring fit on aircraft A inflates scale well above 1.
    d = planes["A"]
    f = consts["A"] + d + drift
    A = np.column_stack([np.ones_like(d), d])
    naive_scale = np.linalg.lstsq(A, f, rcond=None)[0][1]
    assert naive_scale > 1.3


def test_joint_fit_falls_back_to_independent_for_single_aircraft():
    store = TrackStore()
    for i in range(20):
        store._inject("solo", float(i), 1000.0 + i, -100.0 + 5.0 * i, 0.0, 0.0, 270.0)
    fits = store.joint_fit(min_aircraft=2)
    assert "solo" in fits and fits["solo"] is not None


def test_trackstore_has_lock():
    import threading
    store = TrackStore()
    assert isinstance(store.lock, type(threading.Lock()))
    # usable as a context manager
    with store.lock:
        store.update_callsign("abc", "X")
    assert store.latest("abc")["flight"] == "X"


def test_prune_removes_stale_aircraft():
    store = TrackStore(max_age=60.0)
    # "old" last heard at t=10; "fresh" last heard at t=100
    store.update_position("old", 10.0, 1.0, 2.0, 10000.0)
    store.update_velocity("old", 10.0, 400.0, 90.0, 0.0)
    store._inject("old", 10.0, 100.0, 50.0, 1.0, 2.0, 90.0)
    store.update_position("fresh", 100.0, 1.0, 2.0, 10000.0)
    store._inject("fresh", 100.0, 100.0, 50.0, 1.0, 2.0, 90.0)
    # now = 100: "old" (heard at 10) is 90s stale > 60s; "fresh" is current
    store.prune(100.0)
    assert "old" not in store.icaos()
    assert "old" not in store.latest("old")  # state cleared too -> empty dict
    assert store.latest("old") == {}
    assert "fresh" in store.icaos()
    assert store.latest("fresh")["lat"] == 1.0


def test_prune_keeps_recent_within_max_age():
    store = TrackStore(max_age=60.0)
    store.update_position("a", 50.0, 1.0, 2.0, 10000.0)
    store._inject("a", 50.0, 100.0, 50.0, 1.0, 2.0, 90.0)
    store.prune(100.0)  # heard at 50, now 100 -> 50s <= 60s, keep
    assert "a" in store.icaos()
