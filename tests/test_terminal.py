import numpy as np
from doppler1090.track import TrackStore
from doppler1090.terminal import build_rows, build_table, confidence


def _seed(store, icao, n, trk):
    for i in range(n):
        store.update_position(icao, float(i), 0.0, 0.01 + 0.0001 * i, 10000.0)
        store.update_velocity(icao, float(i), 480.0, trk, 0.0)
        s = store.latest(icao)
        store._inject(icao, float(i), 1000.0 + 5.0 * i, -200.0 + 10.0 * i,
                      s["lat"], s["lon"], trk)


def _seed_tracking(store, icao, n=40, span=600.0, noise=0.0, seed=0):
    """Seed an aircraft whose measured offset tracks a swept (S-curve-ish)
    predicted Doppler, giving high correlation and observability."""
    rng = np.random.default_rng(seed)
    for i in range(n):
        d = 0.5 * span * np.cos(np.pi * i / (n - 1))  # sweeps +span/2 .. -span/2
        f = 5000.0 + d + rng.normal(0, noise)
        store._inject(icao, float(i), f, d, 0.0, 0.01 + 0.0001 * i, 270.0)


def test_build_rows_uses_stable_first_seen_order():
    store = TrackStore()
    _seed(store, "first", 40, 0.0)     # seeded first
    _seed(store, "second", 40, 270.0)  # seeded second, higher geometry quality
    rows = build_rows(store, (0.0, 0.0, 0.0))
    # Order follows first-seen (insertion), NOT quality - so rows don't reshuffle.
    assert [r.icao for r in rows] == ["first", "second"]


def test_confidence_high_for_tracking_low_for_flat():
    store = TrackStore()
    _seed_tracking(store, "good", span=600.0, noise=10.0)
    _seed_tracking(store, "flat", span=20.0, noise=200.0)
    good = confidence(store.fit("good"))
    flat = confidence(store.fit("flat"))
    assert good > 0.8
    assert flat < 0.25


def test_min_conf_hides_low_confidence_aircraft():
    store = TrackStore()
    _seed_tracking(store, "good", span=600.0, noise=10.0)
    _seed_tracking(store, "flat", span=20.0, noise=200.0)
    icaos = [r.icao for r in build_rows(store, (0.0, 0.0, 0.0), min_conf=0.25)]
    assert "good" in icaos
    assert "flat" not in icaos
    # show-all (min_conf=0) keeps both
    all_icaos = [r.icao for r in build_rows(store, (0.0, 0.0, 0.0), min_conf=0.0)]
    assert set(all_icaos) == {"good", "flat"}


def test_build_table_columns_and_rows():
    store = TrackStore()
    _seed_tracking(store, "good", span=600.0, noise=10.0)
    rows = build_rows(store, (0.0, 0.0, 0.0))
    table = build_table(rows)
    assert table.row_count == len(rows)
    assert len(table.columns) == 10  # added Conf column
