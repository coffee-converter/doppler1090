import json
import urllib.error
import urllib.parse

import pytest

import doppler1090.aircraft as aircraft
from doppler1090.aircraft import TypeStore, _norm_type


def _store(tmp_path):
    return TypeStore(str(tmp_path / "types.db"))


class _FakeResp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stub_urlopen(monkeypatch, payload):
    monkeypatch.setattr(aircraft.urllib.request, "urlopen",
                        lambda req, timeout=0: _FakeResp(payload))


# ---- _norm_type -----------------------------------------------------------

def test_norm_type_strips_corporate_suffixes():
    assert _norm_type("CIRRUS DESIGN CORP", "SR22T") == "CIRRUS SR22T"


def test_norm_type_collapses_whitespace_and_uppercases():
    assert _norm_type("  boeing  co ", "737-800") == "BOEING 737-800"


def test_norm_type_none_when_empty_after_stripping():
    assert _norm_type(None, None) is None
    assert _norm_type("INC", "   ") is None


# ---- _wiki_photo ----------------------------------------------------------

def _commons_payload():
    return {"batchcomplete": "", "query": {"pages": {
        "999": {  # lower search rank, listed first in dict to test ordering
            "index": 2, "title": "File:Other.svg",
            "imageinfo": [{"mime": "image/svg+xml",
                           "thumburl": "https://upload/other.svg",
                           "descriptionurl": "https://commons/File:Other.svg",
                           "extmetadata": {}}]},
        "123": {
            "index": 1, "title": "File:Cirrus SR22.jpg",
            "imageinfo": [{"mime": "image/jpeg",
                           "thumburl": "https://upload/thumb.jpg",
                           "descriptionurl": "https://commons/File:Cirrus_SR22.jpg",
                           "extmetadata": {"Artist": {
                               "value": "<a href='/x'>Jane Doe</a>"}}}]},
    }}}


def test_wiki_photo_parses_first_photo_and_strips_html_credit(tmp_path, monkeypatch):
    _stub_urlopen(monkeypatch, _commons_payload())
    info = _store(tmp_path)._wiki_photo("CIRRUS SR22T")
    assert info == {"url": "https://upload/thumb.jpg",
                    "link": "https://commons/File:Cirrus_SR22.jpg",
                    "by": "Jane Doe"}


def test_wiki_photo_skips_non_photo_mimes(tmp_path, monkeypatch):
    payload = {"query": {"pages": {"1": {
        "index": 1, "imageinfo": [{"mime": "image/svg+xml",
                                   "thumburl": "https://upload/logo.svg",
                                   "descriptionurl": "x", "extmetadata": {}}]}}}}
    _stub_urlopen(monkeypatch, payload)
    assert _store(tmp_path)._wiki_photo("LOGO CO") is None


def test_wiki_photo_biases_query_toward_real_aircraft(tmp_path, monkeypatch):
    seen = {}

    def capture(req, timeout=0):
        seen["url"] = req.full_url
        return _FakeResp({"batchcomplete": ""})

    monkeypatch.setattr(aircraft.urllib.request, "urlopen", capture)
    _store(tmp_path)._wiki_photo("CIRRUS SR22T")
    # "aircraft" is appended so RC-model / toy photos don't outrank real ones
    assert "aircraft" in urllib.parse.unquote(seen["url"])


def test_wiki_photo_none_on_empty_results(tmp_path, monkeypatch):
    _stub_urlopen(monkeypatch, {"batchcomplete": ""})   # no query key at all
    assert _store(tmp_path)._wiki_photo("NOPE ZZZ") is None


def test_wiki_photo_raises_on_server_error(tmp_path, monkeypatch):
    def boom(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)
    monkeypatch.setattr(aircraft.urllib.request, "urlopen", boom)
    with pytest.raises(urllib.error.HTTPError):
        _store(tmp_path)._wiki_photo("X Y")


# ---- get_photo fallback ordering + caching --------------------------------

def test_reg_photo_takes_precedence_over_type_photo(tmp_path):
    store = _store(tmp_path)
    store._photo_mem["N1"] = {"url": "real", "link": "l", "by": "b"}
    store._type_photo_mem[_norm_type("CIRRUS", "SR22T")] = {
        "url": "type", "link": "", "by": ""}
    info = {"make": "CIRRUS", "model": "SR22T", "reg": "N1"}
    assert store.get_photo("N1", info)["url"] == "real"


def test_get_photo_falls_back_to_type_photo_on_reg_miss(tmp_path):
    store = _store(tmp_path)
    store._photo_mem["N26MR"] = None                    # reg confirmed no photo
    store._type_photo_mem[_norm_type("CIRRUS DESIGN CORP", "SR22T")] = {
        "url": "u", "link": "l", "by": "b"}
    info = {"make": "CIRRUS DESIGN CORP", "model": "SR22T", "reg": "N26MR"}
    assert store.get_photo("N26MR", info) == {"url": "u", "link": "l", "by": "b"}


def test_type_photo_served_from_cache_without_refetch(tmp_path, monkeypatch):
    store = _store(tmp_path)
    calls = []

    def fake_wiki(query):
        calls.append(query)
        return {"url": "u", "link": "l", "by": "b"}

    monkeypatch.setattr(store, "_wiki_photo", fake_wiki)
    key = _norm_type("CIRRUS", "SR22T")
    info, source = store._resolve_type_photo(key)
    assert source == "wikimedia" and info["url"] == "u"
    store._type_photo_mem[key] = info                   # as the worker would
    # a cached type is returned with no further HTTP
    assert store._type_photo("CIRRUS", "SR22T") == info
    assert len(calls) == 1


def test_resolve_type_photo_caches_miss(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(store, "_wiki_photo", lambda q: None)
    info, source = store._resolve_type_photo("NOPE ZZZ")
    assert info is None and source == "none"


def test_retry_after_parses_delta_and_falls_back():
    from doppler1090.aircraft import _retry_after

    class E:
        def __init__(self, h):
            self.headers = h

    assert _retry_after(E({"Retry-After": "30"}), 60) == 30
    assert _retry_after(E(None), 60) == 60          # no headers -> default
    assert _retry_after(E({}), 60) == 60            # header absent -> default
    assert _retry_after(E({"Retry-After": "Wed, 21 Oct 2099 GMT"}), 60) == 60  # date form
    assert _retry_after(E({"Retry-After": "99999"}), 60) == 3600   # clamped


def test_get_json_parks_source_on_429_then_short_circuits(tmp_path, monkeypatch):
    import time
    store = TypeStore(str(tmp_path / "t.db"))

    def boom(req, timeout=8):
        raise urllib.error.HTTPError(req.full_url, 429, "slow down",
                                     {"Retry-After": "45"}, None)

    monkeypatch.setattr(aircraft.urllib.request, "urlopen", boom)
    with pytest.raises(urllib.error.HTTPError):
        store._get_json("https://api.adsbdb.com/x", "adsbdb")
    assert store._source_until["adsbdb"] > time.time() + 30      # parked ~45s

    # while parked, another call raises WITHOUT touching the network
    monkeypatch.setattr(aircraft.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("hit network while parked"))
    with pytest.raises(RuntimeError):
        store._get_json("https://api.adsbdb.com/x", "adsbdb")


def test_enqueue_skips_item_during_backoff(tmp_path):
    import time
    store = TypeStore(str(tmp_path / "t.db"))
    item = ("type", "ABC123")
    store._retry_at[item] = time.time() + 100        # backed off after a failure
    store._enqueue(item)
    assert item not in store._queued and store._q.qsize() == 0


# ---- hexdb.io fallback ----------------------------------------------------

_HEXDB = {"ModeS": "424724", "Registration": "VP-CCQ",
          "Manufacturer": "Bombardier", "ICAOTypeCode": "GL7T",
          "Type": "Global 7500", "RegisteredOwners": "YouJet Management Ltd"}


def test_hexdb_parses_record(tmp_path, monkeypatch):
    _stub_urlopen(monkeypatch, _HEXDB)
    assert _store(tmp_path)._hexdb("424724") == {
        "make": "Bombardier", "model": "Global 7500",
        "reg": "VP-CCQ", "type": "GL7T"}


def test_hexdb_miss_returns_none(tmp_path, monkeypatch):
    def raise404(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
    monkeypatch.setattr(aircraft.urllib.request, "urlopen", raise404)
    assert _store(tmp_path)._hexdb("000001") is None


def test_resolve_falls_back_to_hexdb_when_adsbdb_misses(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(store, "_adsbdb", lambda icao: None)   # adsbdb has nothing
    monkeypatch.setattr(store, "_hexdb", lambda icao: {
        "make": "Bombardier", "model": "Global 7500",
        "reg": "VP-CCQ", "type": "GL7T"})
    info, source = store._resolve("424724")
    assert source == "hexdb"
    assert info["reg"] == "VP-CCQ" and info["type"] == "GL7T"


def test_stale_cache_misses_cleared_on_version_bump(tmp_path):
    import sqlite3
    p = str(tmp_path / "t.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE type_cache (icao TEXT PRIMARY KEY, make TEXT, "
              "model TEXT, reg TEXT, type TEXT, source TEXT, ts REAL)")
    c.execute("INSERT INTO type_cache VALUES ('424724',NULL,NULL,NULL,NULL,'none',0)")
    c.execute("INSERT INTO type_cache VALUES ('ABC123','Boeing','737','N1','B738','adsbdb',0)")
    c.execute("PRAGMA user_version = 1")            # a pre-hexdb cache
    c.commit(); c.close()
    store = TypeStore(p)
    assert "424724" not in store._mem              # stale miss dropped -> re-resolves
    assert store._mem.get("ABC123") == {           # real hit kept
        "make": "Boeing", "model": "737", "reg": "N1", "type": "B738"}
