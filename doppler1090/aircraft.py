"""Aircraft make/model lookup with a persistent cache.

Make/model is not in the ADS-B signal, so it is resolved from the 24-bit ICAO
address against external data and cached to SQLite in the data dir - instant on
later runs, and shared across sessions and browser tabs. Sources, in order:

  1. adsbdb.com  - free JSON API, no key, good global coverage.
  2. FAA registry - offline; authoritative for US-registered aircraft, so it
     covers the private/GA tails the API misses. Opt-in (~73 MB one-time
     download), keyed directly by the Mode S hex.

Lookups run on a background thread so they never block snapshot building: a
miss returns None now and fills in on a later snapshot once resolved. Genuine
"unknown aircraft" answers are cached as misses; transient failures (timeouts,
rate limits) are retried with exponential backoff, and a 429/503 parks that
source for its Retry-After window - so a busy dashboard can't hammer the free
public APIs.
"""

import csv
import html
import io
import json
import queue
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ADSBDB = "https://api.adsbdb.com/v0/aircraft/{}"
FAA_ZIP = "https://registry.faa.gov/database/ReleasableAircraft.zip"
# planespotters photo API, keyed by registration; its terms require a UA with a
# contact URL, and photos link back to the photo page (credit the photographer).
PLANESPOTTERS = "https://api.planespotters.net/pub/photos/reg/{}"
# airport-data.com is the fallback photo source (different pool; some GA tails
# planespotters lacks). Returns a thumbnail, a photo-page link and a credit.
AIRPORTDATA = "https://airport-data.com/api/ac_thumb.json?r={}&n=1"
# Wikimedia Commons: the last-resort photo source, keyed by make/model rather
# than tail. When a specific aircraft has no photo anywhere, a public-domain / CC
# image of the *type* is a useful stand-in. No API key; requires a descriptive UA.
WIKI_API = "https://commons.wikimedia.org/w/api.php"
UA = "doppler1090/1.0 (+https://github.com/coffee-converter/doppler1090)"

# Corporate suffixes dropped from the manufacturer name before searching/keying
# so "CIRRUS DESIGN CORP" and adsbdb's "CIRRUS" collapse to one type ("CIRRUS
# SR22T") - both a cleaner Wikimedia query and a shared cache key across sources.
_CORP_SUFFIXES = {"DESIGN", "CORP", "CORPORATION", "INC", "CO", "LLC", "LTD",
                  "COMPANY", "AVIATION", "AIRCRAFT", "INDUSTRIES", "GMBH",
                  "AG", "SA"}
_TAG = re.compile(r"<[^>]+>")


def _norm_type(make, model):
    """Normalized make/model key ("CIRRUS SR22T"), or None if empty. Strips
    corporate suffixes from the make; used as both Wikimedia query and cache key."""
    words = [w for w in (make or "").upper().split() if w not in _CORP_SUFFIXES]
    words += (model or "").upper().split()
    return " ".join(words) or None


def _strip_html(s):
    """Plain-text credit from a Wikimedia extmetadata Artist value (often an
    <a> tag): drop tags and unescape entities."""
    return html.unescape(_TAG.sub("", s or "")).strip()


def _retry_after(exc, default):
    """Seconds to wait from a 429/503 ``Retry-After`` header. Handles the
    delta-seconds form; the rarer HTTP-date form falls back to ``default``.
    Clamped to [1, 3600]."""
    val = exc.headers.get("Retry-After") if exc.headers else None
    try:
        secs = int(val)
    except (TypeError, ValueError):
        secs = default
    return max(1, min(secs, 3600))


class TypeStore:
    def __init__(self, path, use_faa=False):
        self.use_faa = use_faa
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS type_cache (icao TEXT PRIMARY KEY, "
            "make TEXT, model TEXT, reg TEXT, type TEXT, source TEXT, ts REAL)")
        try:              # add the ICAO type column to a pre-existing cache
            self._conn.execute("ALTER TABLE type_cache ADD COLUMN type TEXT")
        except sqlite3.OperationalError:
            pass
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS faa (icao TEXT PRIMARY KEY, "
            "reg TEXT, make TEXT, model TEXT)")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS photo_cache (reg TEXT PRIMARY KEY, "
            "url TEXT, link TEXT, by TEXT, source TEXT, ts REAL)")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS type_photo_cache (key TEXT PRIMARY KEY, "
            "url TEXT, link TEXT, by TEXT, source TEXT, ts REAL)")
        self._conn.commit()
        self._lock = threading.Lock()   # guards all _conn access
        self._mem = {}                  # icao -> {make,model,reg,type} | None
        for icao, make, model, reg, typ, source in self._conn.execute(
                "SELECT icao, make, model, reg, type, source FROM type_cache"):
            self._mem[icao] = None if source == "none" else {
                "make": make, "model": model, "reg": reg, "type": typ}
        self._photo_mem = {}            # reg -> {url,link,by} | None (miss)
        for reg, url, link, by, source in self._conn.execute(
                "SELECT reg, url, link, by, source FROM photo_cache"):
            self._photo_mem[reg] = None if source == "none" else {
                "url": url, "link": link, "by": by}
        self._type_photo_mem = {}       # make/model key -> {url,link,by} | None
        for key, url, link, by, source in self._conn.execute(
                "SELECT key, url, link, by, source FROM type_photo_cache"):
            self._type_photo_mem[key] = None if source == "none" else {
                "url": url, "link": link, "by": by}
        # queue items: ("type", icao) | ("photo", reg) | ("typephoto", key)
        self._q = queue.Queue()
        self._queued = set()            # in-flight items (avoid duplicates)
        self._adsb_missed = set()       # tried adsbdb already, awaiting FAA
        self._source_until = {}         # source -> epoch it's parked until (429/503)
        self._retry_at = {}             # (kind,key) -> earliest retry epoch (backoff)
        self._retry_n = {}              # (kind,key) -> consecutive transient count
        self._faa_ready = self._count("faa") > 0
        threading.Thread(target=self._worker, daemon=True).start()
        if self.use_faa and not self._faa_ready:
            threading.Thread(target=self._import_faa, daemon=True).start()

    def get(self, icao):
        """Cached {make,model,reg} or None; schedules a lookup on a miss."""
        icao = icao.upper()
        if icao in self._mem:
            return self._mem[icao]
        self._enqueue(("type", icao))
        return None

    def get_photo(self, reg, info=None):
        """Cached photo {url,link,by} or None; schedules a lookup on a miss. When
        the tail has no photo in any reg-keyed source, falls back to a photo of
        the aircraft's make/model (from ``info``), if available."""
        reg = reg.upper()
        if reg not in self._photo_mem:
            self._enqueue(("photo", reg))
            return None
        photo = self._photo_mem[reg]
        if photo is not None:
            return photo
        # reg confirmed to have no photo -> stand in with a make/model type photo
        if info:
            return self._type_photo(info.get("make"), info.get("model"))
        return None

    def _type_photo(self, make, model):
        """Cached make/model photo {url,link,by} or None; schedules a Wikimedia
        lookup on a miss. Shared across every tail of the same type."""
        key = _norm_type(make, model)
        if key is None:
            return None
        if key in self._type_photo_mem:
            return self._type_photo_mem[key]
        self._enqueue(("typephoto", key))
        return None

    def _enqueue(self, item):
        if item in self._queued:
            return
        ra = self._retry_at.get(item)
        if ra is not None and time.time() < ra:
            return                      # negatively cached after a transient failure
        self._queued.add(item)
        self._q.put(item)

    # ---- background resolution -------------------------------------------
    def _worker(self):
        while True:
            kind, key = self._q.get()
            info = source = None
            try:
                if kind == "type":
                    info, source = self._resolve(key)
                elif kind == "photo":
                    info, source = self._resolve_photo(key)
                else:                    # "typephoto": make/model key
                    info, source = self._resolve_type_photo(key)
            except Exception:
                source = None            # transient: leave unresolved, retry later
            if source is not None:       # definitive answer (hit or real miss)
                if kind == "type":
                    self._store(key, info, source); self._mem[key] = info
                elif kind == "photo":
                    self._store_photo(key, info, source); self._photo_mem[key] = info
                else:
                    self._store_type_photo(key, info, source)
                    self._type_photo_mem[key] = info
                self._retry_at.pop((kind, key), None)
                self._retry_n.pop((kind, key), None)
            else:                        # transient: exponential backoff (5s..10min)
                n = self._retry_n.get((kind, key), 0) + 1
                self._retry_n[(kind, key)] = n
                self._retry_at[(kind, key)] = time.time() + min(600, 5 * 2 ** (n - 1))
            self._queued.discard((kind, key))
            time.sleep(0.3)              # be polite to the API

    def _resolve(self, icao):
        """Return ({make,model,reg}, source) for a hit, (None, 'none') for a
        confirmed miss, or (None, None) to retry later (transient / FAA not yet
        imported)."""
        if icao not in self._adsb_missed:
            hit = self._adsbdb(icao)     # may raise on transient error -> retry
            if hit:
                return hit, "adsbdb"
            self._adsb_missed.add(icao)  # confirmed adsbdb miss
        if self.use_faa:
            if not self._faa_ready:
                return None, None        # wait for the registry import
            r = self._faa_get(icao)
            if r:
                return r, "faa"
        return None, "none"

    def _get_json(self, url, source):
        """GET JSON with the shared UA. Returns the parsed body, or None on 404
        (a genuine miss). On 429/503 it parks ``source`` for its Retry-After
        window; any non-404 error - including a parked source - raises so the
        caller retries later with backoff."""
        if time.time() < self._source_until.get(source, 0):
            raise RuntimeError(f"{source} backing off (rate-limited)")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 503):
                self._source_until[source] = time.time() + _retry_after(e, 60)
            raise

    def _adsbdb(self, icao):
        j = self._get_json(ADSBDB.format(icao.lower()), "adsbdb")
        if j is None:
            return None                  # genuinely unknown
        resp = (j or {}).get("response")
        if isinstance(resp, dict) and resp.get("aircraft"):
            ac = resp["aircraft"]
            return {"make": ac.get("manufacturer"), "model": ac.get("type"),
                    "reg": ac.get("registration"), "type": ac.get("icao_type")}
        return None                      # "unknown aircraft"

    def _faa_get(self, icao):
        with self._lock:
            r = self._conn.execute(
                "SELECT reg, make, model FROM faa WHERE icao = ?", (icao,)
            ).fetchone()
        return {"reg": r[0], "make": r[1], "model": r[2], "type": None} if r else None

    def _store(self, icao, info, source):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO type_cache "
                "(icao, make, model, reg, type, source, ts) VALUES (?,?,?,?,?,?,?)",
                (icao, info and info["make"], info and info["model"],
                 info and info["reg"], info and info.get("type"), source,
                 time.time()))
            self._conn.commit()

    def _resolve_photo(self, reg):
        """Try each photo source in turn; return (info, source) on the first
        hit, (None, 'none') if all confirm no photo. A transient error in any
        source raises out so the whole lookup retries later."""
        for fetch, source in ((self._ps_photo, "planespotters"),
                              (self._ad_photo, "airport-data")):
            info = fetch(reg)            # None => no photo; raises on transient
            if info:
                return info, source
        return None, "none"

    def _ps_photo(self, reg):
        j = self._get_json(PLANESPOTTERS.format(reg), "planespotters")
        if j is None:
            return None
        photos = (j or {}).get("photos") or []
        if photos:
            p = photos[0]
            thumb = p.get("thumbnail_large") or p.get("thumbnail") or {}
            return {"url": thumb.get("src"), "link": p.get("link"),
                    "by": p.get("photographer")}
        return None

    def _ad_photo(self, reg):
        j = self._get_json(AIRPORTDATA.format(reg), "airport-data")
        if j is None:                        # airport-data returns 404 for misses
            return None
        data = (j or {}).get("data") or []
        if data:
            d = data[0]
            return {"url": d.get("image"), "link": d.get("link"),
                    "by": d.get("photographer")}
        return None

    def _store_photo(self, reg, info, source):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO photo_cache (reg, url, link, by, source, "
                "ts) VALUES (?,?,?,?,?,?)",
                (reg, info and info["url"], info and info["link"],
                 info and info["by"], source, time.time()))
            self._conn.commit()

    def _resolve_type_photo(self, query):
        """(info, 'wikimedia') on a hit, (None, 'none') if Commons has no usable
        photo. A transient error raises out so the lookup retries later."""
        info = self._wiki_photo(query)   # None => no photo; raises on transient
        return (info, "wikimedia") if info else (None, "none")

    def _wiki_photo(self, query):
        """First usable Commons photo for a make/model query, as {url,link,by},
        or None. Searches the File namespace and takes the top-ranked JPEG/PNG."""
        # "aircraft" biases search relevance toward real airframes so RC-model and
        # toy photos (whose filenames match the type exactly) don't outrank them.
        params = {"action": "query", "format": "json", "generator": "search",
                  "gsrnamespace": 6, "gsrsearch": query + " aircraft", "gsrlimit": 8,
                  "prop": "imageinfo", "iiprop": "url|extmetadata|mime",
                  "iiurlwidth": 640}
        j = self._get_json(WIKI_API + "?" + urllib.parse.urlencode(params),
                           "wikimedia")
        if j is None:
            return None
        pages = ((j or {}).get("query") or {}).get("pages") or {}
        for p in sorted(pages.values(), key=lambda p: p.get("index", 0)):
            ii = (p.get("imageinfo") or [{}])[0]
            if ii.get("mime") in ("image/jpeg", "image/png") and ii.get("thumburl"):
                artist = ((ii.get("extmetadata") or {}).get("Artist") or {})
                return {"url": ii["thumburl"], "link": ii.get("descriptionurl"),
                        "by": _strip_html(artist.get("value")) or None}
        return None

    def _store_type_photo(self, key, info, source):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO type_photo_cache "
                "(key, url, link, by, source, ts) VALUES (?,?,?,?,?,?)",
                (key, info and info["url"], info and info["link"],
                 info and info["by"], source, time.time()))
            self._conn.commit()

    def _count(self, table):
        return self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # ---- FAA registry import (one time) ----------------------------------
    def _import_faa(self):
        try:
            print("doppler1090: downloading FAA aircraft registry "
                  "(~73 MB, one time)...")
            req = urllib.request.Request(FAA_ZIP, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=300) as r:
                zf = zipfile.ZipFile(io.BytesIO(r.read()))
            ref = {}                     # MFR MDL CODE -> (make, model)
            with zf.open("ACFTREF.txt") as f:
                for row in csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")):
                    ref[row["CODE"].strip()] = (row["MFR"].strip(),
                                                row["MODEL"].strip())
            rows = []
            with zf.open("MASTER.txt") as f:
                for row in csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")):
                    hexid = row["MODE S CODE HEX"].strip().upper()
                    mm = ref.get(row["MFR MDL CODE"].strip())
                    if hexid and mm:
                        rows.append((hexid, "N" + row["N-NUMBER"].strip(),
                                     mm[0], mm[1]))
            with self._lock:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO faa (icao, reg, make, model) "
                    "VALUES (?,?,?,?)", rows)
                self._conn.commit()
            self._faa_ready = True
            print(f"doppler1090: FAA registry imported ({len(rows)} aircraft)")
        except Exception as e:
            print(f"doppler1090: FAA registry import failed ({e}); "
                  "using adsbdb only")
            self.use_faa = False         # finalize adsbdb-missed as real misses
