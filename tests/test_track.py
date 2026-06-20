import numpy as np
from doppler1090.track import TrackStore


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
