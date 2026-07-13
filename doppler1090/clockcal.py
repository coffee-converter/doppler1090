"""Receiver oscillator calibration, accumulated across sessions.

The joint clock fit gives each aircraft a constant = the receiver's LO offset
plus that transmitter's own offset. Those transmitter offsets scatter around
zero, so the *median* across many aircraft estimates the receiver offset alone -
the residual you'd null out with ``--ppm``. A single 60 s frame sees only a few
aircraft and is noisy, so we keep one constant per tail (persisted) and report a
robust median over a rolling window. 1 ppm at 1090 MHz = 1090 Hz.

Every sample is tagged with the ``--ppm`` in force when it was taken. Since that
correction is applied at the SDR, the measured constant already has it folded in,
so the *absolute* crystal offset is ``measured + applied_ppm`` - invariant to the
setting. Storing the tag lets us reconstruct the absolute offset, so samples from
any ``--ppm`` accumulate together instead of skewing each other."""

import sqlite3
import statistics
import threading

WINDOW_S = 86400.0     # 24 h: averages many aircraft, tracks day-scale drift
_FLUSH_S = 30.0        # persist at most this often (observe runs many times/s)


class ClockCal:
    def __init__(self, path, ppm=0, window_s=WINDOW_S):
        self.ppm = int(ppm)
        self.window_s = window_s
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS clock_ac (icao TEXT, ppm INTEGER, "
            "offset_hz REAL, ts REAL, PRIMARY KEY (icao, ppm))")
        self._conn.commit()
        self._lock = threading.Lock()
        self._ac = {}          # (icao, ppm) -> [offset_hz, ts]
        self._dirty = set()
        self._last_flush = 0.0
        for icao, ppm_, off, ts in self._conn.execute(
                "SELECT icao, ppm, offset_hz, ts FROM clock_ac"):
            self._ac[(icao, ppm_)] = [off, ts]

    def observe(self, consts, now):
        """Record each aircraft's current constant (tagged with the live --ppm)."""
        if not consts:
            return
        with self._lock:
            for icao, off in consts.items():
                key = (icao, self.ppm)
                self._ac[key] = [off, now]
                self._dirty.add(key)
            if now - self._last_flush >= _FLUSH_S:
                self._flush(now)

    def _flush(self, now):
        self._conn.executemany(
            "INSERT OR REPLACE INTO clock_ac (icao, ppm, offset_hz, ts) "
            "VALUES (?,?,?,?)",
            [(ic, pp, self._ac[(ic, pp)][0], self._ac[(ic, pp)][1])
             for (ic, pp) in self._dirty])
        self._conn.commit()
        self._dirty.clear()
        self._last_flush = now

    def estimate(self, now):
        """Robust absolute crystal offset: median of (measured + applied_ppm)
        over the window across every --ppm setting (falling back to all-time if
        the window is sparse)."""
        with self._lock:
            samples = [(v[0] + pp * 1090.0, v[1])      # absolute Hz = measured + applied
                       for (ic, pp), v in self._ac.items()]
        fresh = [o for o, ts in samples if now - ts <= self.window_s]
        rows = fresh if len(fresh) >= 3 else [o for o, _ in samples]
        if not rows:
            return None
        return {"offset_ppm": statistics.median(rows) / 1090.0,   # absolute offset
                "n_aircraft": len(rows),
                "window_h": round(self.window_s / 3600.0),
                "ppm": self.ppm}                          # current --ppm in force
