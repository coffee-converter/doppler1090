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
