from doppler1090.history import Recorder, History, read_session_meta


def _make_session(path):
    """A minimal recorded session: one aircraft, two bursts 5 s apart."""
    rec = Recorder(path, (40.0, -75.0, 100.0), started_at=1000.0, ppm=3)
    rec.log_state(1000.0, "abc123", lat=40.1, lon=-75.1, alt=10000,
                  speed_kt=400, track=90, vrate_fpm=0, flight="TEST1")
    rec.log_burst(1000.0, "abc123", 50.0, 45.0, 40.1, -75.1, 90, signal=-10.0)
    rec.log_burst(1005.0, "abc123", 40.0, 35.0, 40.1, -75.1, 90, signal=-10.0)
    rec.flush()
    rec.close()


def test_read_session_meta_returns_rx_and_ppm(tmp_path):
    p = str(tmp_path / "s.sqlite")
    _make_session(p)
    rx_llh, ppm = read_session_meta(p)
    assert rx_llh == (40.0, -75.0, 100.0)   # (lat, lon, alt_metres)
    assert ppm == 3


def test_bounds_is_session_extent_not_wallclock(tmp_path):
    p = str(tmp_path / "s.sqlite")
    _make_session(p)
    lo, hi = History(p, max_age=60).bounds()
    assert lo == 1000.0
    assert hi == 1005.0
