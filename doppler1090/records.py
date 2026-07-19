"""All-time flight records, persisted across sessions.

A tiny SQLite table in the data dir that remembers the extremes this receiver
has ever seen - fastest/slowest, highest/lowest, biggest climb/descent,
farthest/nearest, strongest/weakest signal, and widest Doppler swing - with the
flight, tail, and type that set each, and when. Folded in from live snapshots;
survives restarts, so the numbers are genuinely all-time."""

import sqlite3
import threading

# metric -> direction: whether a bigger or smaller value is the "record".
_METRICS = {"speed_kt": "max", "speed_min_kt": "min",       # fastest / slowest
            "alt_ft": "max", "alt_min_ft": "min",           # highest / lowest
            "vrate_max_fpm": "max", "vrate_min_fpm": "min",  # climb / descent
            "range_nm": "max", "closest_nm": "min",          # farthest / nearest
            "sig_max_db": "max", "sig_min_db": "min",        # strongest / weakest
            "dop_span_hz": "max"}                             # widest Doppler swing
# metrics where only a strictly positive value is meaningful (0/None is just
# missing data). Signal (dBFS) and vertical rate can legitimately be negative.
_POSITIVE = {"speed_kt", "speed_min_kt", "alt_ft", "alt_min_ft",
             "range_nm", "closest_nm", "dop_span_hz"}

_FIELDS = ("flight", "reg", "icao", "make", "model")   # identity carried per record


class Records:
    def __init__(self, path):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS records (metric TEXT PRIMARY KEY, "
            "value REAL, flight TEXT, reg TEXT, icao TEXT, make TEXT, model TEXT, "
            "ts REAL)")
        for col in ("reg", "make", "model"):     # migrate a pre-existing table
            try:
                self._conn.execute(f"ALTER TABLE records ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        self._conn.commit()
        self._lock = threading.Lock()
        self._last_backfill = 0.0        # throttle for identity backfill
        self._rec = {}   # metric -> {value, flight, reg, icao, make, model, ts}
        for row in self._conn.execute(
                "SELECT metric, value, flight, reg, icao, make, model, ts "
                "FROM records"):
            metric, value = row[0], row[1]
            self._rec[metric] = dict(zip(_FIELDS, row[2:7]),
                                     value=value, ts=row[7])

    def _update(self, metric, value, who, now):
        if value is None or (value <= 0 and metric in _POSITIVE):
            return
        cur = self._rec.get(metric)
        if cur is not None:
            better = (value > cur["value"] if _METRICS[metric] == "max"
                      else value < cur["value"])
            if not better:
                return
        rec = dict(who, value=float(value), ts=now)
        with self._lock:
            self._rec[metric] = rec
            self._conn.execute(
                "INSERT OR REPLACE INTO records (metric, value, flight, reg, "
                "icao, make, model, ts) VALUES (?,?,?,?,?,?,?,?)",
                (metric, rec["value"], *(who[f] for f in _FIELDS), now))
            self._conn.commit()

    def _fill_identity(self, who):
        """A record is often set before the type lookup resolves; once it does,
        patch make/model/reg (and a missing flight) onto records this same
        aircraft still holds."""
        icao = who["icao"]
        dirty = [(m, r) for m, r in self._rec.items()
                 if r.get("icao") == icao and not r.get("make")]
        if not dirty:
            return
        with self._lock:
            for metric, r in dirty:
                for f in ("reg", "make", "model"):
                    r[f] = who[f]
                if who["flight"] and not r.get("flight"):
                    r["flight"] = who["flight"]
                self._conn.execute(
                    "UPDATE records SET flight=?, reg=?, make=?, model=? "
                    "WHERE metric=?",
                    (r.get("flight"), r["reg"], r["make"], r["model"], metric))
            self._conn.commit()

    def observe(self, a, now):
        """Fold one live aircraft's snapshot dict into the records."""
        who = {f: a.get(f) for f in _FIELDS}
        if who["icao"] and (who["make"] or who["model"] or who["reg"]):
            self._fill_identity(who)

        def rec(metric, value):
            self._update(metric, value, who, now)

        spd, alt, vr = a.get("speed_kt"), a.get("alt"), a.get("vrate")
        rec("speed_kt", spd);        rec("speed_min_kt", spd)      # fastest / slowest
        rec("alt_ft", alt);          rec("alt_min_ft", alt)        # highest / lowest
        if vr is not None:           # a climb only sets climb; a descent, descent
            if vr > 0:
                rec("vrate_max_fpm", vr)
            elif vr < 0:
                rec("vrate_min_fpm", vr)
        rng = a.get("range_km")
        rng_nm = rng / 1.852 if rng else None   # 0 => no valid position, skip
        rec("range_nm", rng_nm);     rec("closest_nm", rng_nm)     # farthest / nearest
        sig = a.get("rssi")
        rec("sig_max_db", sig);      rec("sig_min_db", sig)        # strongest / weakest
        rec("dop_span_hz", a.get("dop_span"))                       # widest swing

    def backfill(self, get_fn, now, interval=60.0):
        """Fill in blank make/model/reg on records whose aircraft isn't live
        right now (so ``observe``'s ``_fill_identity`` never fires for it) but
        whose ICAO now resolves via ``get_fn`` - a type lookup that schedules on
        a miss and returns ``{make,model,reg}`` once resolved. Throttled to once
        per ``interval`` so it's ~free on the snapshot path."""
        if now - self._last_backfill < interval:
            return
        self._last_backfill = now
        with self._lock:
            blanks = {r["icao"] for r in self._rec.values()
                      if r.get("icao") and not r.get("make")}
        for icao in blanks:
            info = get_fn(icao)          # schedules the lookup on first miss
            if info and (info.get("make") or info.get("model") or info.get("reg")):
                self._fill_identity({"icao": icao, "flight": None,
                                     "reg": info.get("reg"), "make": info.get("make"),
                                     "model": info.get("model")})

    def snapshot(self):
        with self._lock:
            return {m: dict(v) for m, v in self._rec.items()}
