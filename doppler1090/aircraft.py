"""Aircraft make/model lookup with a persistent cache.

Make/model is not in the ADS-B signal, so it is resolved from the 24-bit ICAO
address against external data and cached to SQLite in the data dir - instant on
later runs, and shared across sessions and browser tabs. Sources, in order:

  1. adsbdb.com  - free JSON API, no key, good global coverage.
  2. FAA registry - offline; authoritative for US-registered aircraft, so it
     covers the private/GA tails the API misses. Opt-in (~73 MB one-time
     download), keyed directly by the Mode S hex.

Lookups run on a background thread so they never block snapshot building: a
miss returns None now and fills in on a later snapshot once resolved. Transient
failures (timeouts, rate limits) are NOT cached, so they retry; only genuine
"unknown aircraft" answers are cached as misses.
"""

import csv
import io
import json
import queue
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import zipfile

ADSBDB = "https://api.adsbdb.com/v0/aircraft/{}"
FAA_ZIP = "https://registry.faa.gov/database/ReleasableAircraft.zip"
UA = "doppler1090 aircraft lookup"


class TypeStore:
    def __init__(self, path, use_faa=False):
        self.use_faa = use_faa
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS type_cache (icao TEXT PRIMARY KEY, "
            "make TEXT, model TEXT, reg TEXT, source TEXT, ts REAL)")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS faa (icao TEXT PRIMARY KEY, "
            "reg TEXT, make TEXT, model TEXT)")
        self._conn.commit()
        self._lock = threading.Lock()   # guards all _conn access
        self._mem = {}                  # icao -> {make,model,reg} | None (miss)
        for icao, make, model, reg, source in self._conn.execute(
                "SELECT icao, make, model, reg, source FROM type_cache"):
            self._mem[icao] = None if source == "none" else {
                "make": make, "model": model, "reg": reg}
        self._q = queue.Queue()         # icaos to resolve
        self._queued = set()            # icaos in flight (avoid duplicates)
        self._adsb_missed = set()       # tried adsbdb already, awaiting FAA
        self._faa_ready = self._count("faa") > 0
        threading.Thread(target=self._worker, daemon=True).start()
        if self.use_faa and not self._faa_ready:
            threading.Thread(target=self._import_faa, daemon=True).start()

    def get(self, icao):
        """Cached {make,model,reg} or None; schedules a lookup on a miss."""
        icao = icao.upper()
        if icao in self._mem:
            return self._mem[icao]
        if icao not in self._queued:
            self._queued.add(icao)
            self._q.put(icao)
        return None

    # ---- background resolution -------------------------------------------
    def _worker(self):
        while True:
            icao = self._q.get()
            info = source = None
            try:
                info, source = self._resolve(icao)
            except Exception:
                source = None            # transient: leave unresolved, retry later
            if source is not None:       # definitive answer (hit or real miss)
                self._store(icao, info, source)
                self._mem[icao] = info
            self._queued.discard(icao)
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

    def _adsbdb(self, icao):
        req = urllib.request.Request(ADSBDB.format(icao.lower()),
                                     headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                j = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None              # genuinely unknown
            raise                        # 429/5xx etc -> transient
        resp = (j or {}).get("response")
        if isinstance(resp, dict) and resp.get("aircraft"):
            ac = resp["aircraft"]
            return {"make": ac.get("manufacturer"), "model": ac.get("type"),
                    "reg": ac.get("registration")}
        return None                      # "unknown aircraft"

    def _faa_get(self, icao):
        with self._lock:
            r = self._conn.execute(
                "SELECT reg, make, model FROM faa WHERE icao = ?", (icao,)
            ).fetchone()
        return {"reg": r[0], "make": r[1], "model": r[2]} if r else None

    def _store(self, icao, info, source):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO type_cache "
                "(icao, make, model, reg, source, ts) VALUES (?,?,?,?,?,?)",
                (icao, info and info["make"], info and info["model"],
                 info and info["reg"], source, time.time()))
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
