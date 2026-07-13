"""Antenna coverage: the farthest an aircraft has been heard in each bearing.

Bins the sky into equal bearing sectors and remembers, per sector, the greatest
range at which any aircraft was received - a classic reception "range plot" that
reveals antenna coverage and obstructions. Persisted to the data dir, so it
accumulates all-time across sessions."""

import sqlite3
import threading


class Coverage:
    def __init__(self, path, sectors=72):
        self.sectors = sectors
        self.step = 360.0 / sectors            # degrees per sector
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS coverage (sector INTEGER PRIMARY KEY, "
            "range_nm REAL, ts REAL)")
        self._conn.commit()
        self._lock = threading.Lock()
        self._max = [0.0] * sectors            # sector -> farthest range (nm)
        for sector, rng, _ in self._conn.execute(
                "SELECT sector, range_nm, ts FROM coverage"):
            if 0 <= sector < sectors:
                self._max[sector] = rng or 0.0

    def observe(self, bearing_deg, range_nm, now):
        if range_nm is None or range_nm <= 0:
            return
        i = int((bearing_deg % 360.0) / self.step) % self.sectors
        if range_nm <= self._max[i]:
            return
        with self._lock:
            self._max[i] = range_nm
            self._conn.execute(
                "INSERT OR REPLACE INTO coverage (sector, range_nm, ts) "
                "VALUES (?,?,?)", (i, range_nm, now))
            self._conn.commit()

    def snapshot(self):
        with self._lock:
            return {"step_deg": self.step, "sectors": list(self._max)}
