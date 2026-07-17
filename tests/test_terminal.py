import numpy as np
from doppler1090.track import TrackStore
from doppler1090.terminal import (build_rows, build_table, build_header,
                                  confidence)


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


def test_build_table_full_width_has_all_columns():
    store = TrackStore()
    _seed_tracking(store, "good", span=600.0, noise=10.0)
    rows = build_rows(store, (0.0, 0.0, 0.0))
    table = build_table(rows)                    # width=None -> full
    assert table.row_count == len(rows)
    assert len(table.columns) == 15
    assert [c.header for c in table.columns][:3] == ["ICAO", "Ident", "Aircraft"]


def test_build_table_drops_columns_as_width_narrows():
    store = TrackStore()
    _seed_tracking(store, "good", span=600.0, noise=10.0)
    rows = build_rows(store, (0.0, 0.0, 0.0))
    full = len(build_table(rows, width=200).columns)
    compact = len(build_table(rows, width=120).columns)
    narrow = len(build_table(rows, width=80).columns)
    assert full == 15 and compact == 11 and narrow == 8
    # the fit diagnostics are the first to go; heading survives to narrow
    narrow_headers = [c.header for c in build_table(rows, width=80).columns]
    assert "Trk°" in narrow_headers
    assert "Scale" not in narrow_headers and "Conf" not in narrow_headers
    assert "Dop m(p) Hz" in narrow_headers       # merged Doppler column stays


class _FakeTypeStore:
    def __init__(self, table):
        self._t = table

    def get(self, icao):
        return self._t.get(icao)


def test_build_rows_enriches_make_model_reg_and_signal():
    store = TrackStore()
    # inject with a nonzero burst amplitude so rssi is computed
    for i in range(10):
        store._inject("aa", float(i), 5000.0, 100.0 - 10 * i, 0.0,
                      0.01 + 0.0001 * i, 270.0, signal=0.5)
    ts = _FakeTypeStore({"aa": {"make": "Embraer", "model": "EMB-175 LR",
                                "reg": "N163SY"}})
    r = build_rows(store, (0.0, 0.0, 0.0), type_store=ts)[0]
    assert r.make == "Embraer" and r.model == "EMB-175 LR" and r.reg == "N163SY"
    assert r.rssi is not None                    # signal present -> dBFS computed


def test_build_rows_without_type_store_leaves_identity_blank():
    store = TrackStore()
    _seed_tracking(store, "aa", span=600.0, noise=10.0)
    r = build_rows(store, (0.0, 0.0, 0.0))[0]    # no type_store
    assert r.make is None and r.model is None and r.reg is None
    assert r.rssi is None                          # seeded with signal=0


def test_build_header_shows_status_and_ppm():
    import io
    from rich.console import Console
    status = {"rx": (42.19, -88.19), "uptime_s": 1531, "n_aircraft": 6,
              "burst_rate": 141, "sdr_state": "receiving"}
    clock = {"offset_ppm": -1.8, "ppm": 0, "n_aircraft": 4, "window_h": 24,
             "drift_ppm_min": 0.3, "fresh_n": 4}
    buf = io.StringIO()
    Console(file=buf, width=200).print(build_header(status, clock))
    out = buf.getvalue()
    assert "6 ac" in out and "141 brst/min" in out and "receiving" in out
    assert "-1.8" in out                 # absolute offset ppm
    assert "--ppm -2" in out             # suggestion = round(-1.8)
    assert "ADS-B" in out                # generalized title
