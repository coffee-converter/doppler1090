from doppler1090.records import Records


def _who(**kw):
    base = {"flight": None, "reg": None, "icao": None, "make": None, "model": None}
    base.update(kw)
    return base


def test_backfill_patches_blank_record_identity(tmp_path):
    r = Records(str(tmp_path / "rec.db"))
    r._update("speed_kt", 519, _who(flight="VPCCQ", icao="424724"), 1000.0)
    assert r.snapshot()["speed_kt"]["make"] is None          # blank to start
    resolved = {"424724": {"make": "Bombardier", "model": "Global 7500",
                           "reg": "VP-CCQ", "type": "GL7T"}}
    r.backfill(lambda icao: resolved.get(icao), now=10000.0)
    got = r.snapshot()["speed_kt"]
    assert got["make"] == "Bombardier" and got["reg"] == "VP-CCQ"
    assert got["flight"] == "VPCCQ"                           # flight preserved


def test_backfill_is_throttled(tmp_path):
    r = Records(str(tmp_path / "rec.db"))
    r._update("speed_kt", 519, _who(flight="VPCCQ", icao="424724"), 1000.0)
    calls = []
    get = lambda icao: calls.append(icao) or None
    r.backfill(get, now=100.0)      # first run fires
    r.backfill(get, now=110.0)      # within interval -> skipped
    assert calls == ["424724"]
