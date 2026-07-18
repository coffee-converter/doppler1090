import os

from doppler1090.history import (Recorder, History, read_session_meta,
                                  valid_session_name, list_sessions, find_session)


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


def test_valid_session_name():
    assert valid_session_name("session-20260713-073453.sqlite")
    assert not valid_session_name("records.sqlite")
    assert not valid_session_name("../evil.sqlite")
    assert not valid_session_name("")
    assert not valid_session_name(None)


def _make_session_at(path, t0):
    rec = Recorder(path, (40.0, -75.0, 100.0), started_at=t0, ppm=0)
    rec.log_burst(t0, "xyz789", 1.0, 1.0, 40.0, -75.0, 90, signal=-10.0)
    rec.log_burst(t0 + 5.0, "xyz789", 1.0, 1.0, 40.0, -75.0, 90, signal=-10.0)
    rec.flush()
    rec.close()


def test_find_session_resolves_ts_to_its_file(tmp_path):
    a = "session-20260101-000000.sqlite"
    b = "session-20260102-000000.sqlite"
    _make_session_at(str(tmp_path / a), 1000.0)   # extent 1000..1005
    _make_session_at(str(tmp_path / b), 2000.0)   # extent 2000..2005
    d = str(tmp_path)
    assert find_session(d, 1002.0, 60) == a
    assert find_session(d, 2003.0, 60) == b
    assert find_session(d, 9999.0, 60) is None


def test_list_sessions_ignores_non_session_files(tmp_path):
    _make_session(str(tmp_path / "session-20260101-000000.sqlite"))
    open(str(tmp_path / "records.sqlite"), "w").close()
    names = [os.path.basename(p) for p in list_sessions(str(tmp_path))]
    assert names == ["session-20260101-000000.sqlite"]
