"""One-time calibration backfill from recorded sessions.

Walks the session recordings, re-runs the joint clock fit across each at sampled
instants, and feeds the per-aircraft constants into the ClockCal accumulator -
tagged with the --ppm that session ran at (read from the session, defaulting to
0 for recordings made before sessions stored it). This seeds the oscillator
estimate from history instead of waiting for it to accumulate live."""

import glob
import os
import sqlite3

from .history import History
from .clockcal import ClockCal

_STEP_S = 45.0        # sample interval within each session


def _session_ppm(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        try:
            row = conn.execute("SELECT ppm FROM sessions LIMIT 1").fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except sqlite3.OperationalError:
            return 0          # recording predates the sessions.ppm column
    finally:
        conn.close()


def _span(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT MIN(t), MAX(t) FROM burst_log").fetchone()
    finally:
        conn.close()


def backfill_clockcal(data_dir, max_age=60.0):
    """Seed clockcal.sqlite from every session-*.sqlite in the data dir (and its
    _archive). Returns a summary dict."""
    cc = ClockCal(os.path.join(data_dir, "clockcal.sqlite"))
    paths = sorted(glob.glob(os.path.join(data_dir, "session-*.sqlite")) +
                   glob.glob(os.path.join(data_dir, "_archive", "session-*.sqlite")))
    n_sessions = frames = 0
    for path in paths:
        lo, hi = _span(path)
        if lo is None:
            continue
        ppm = _session_ppm(path)
        hist = History(path, max_age=max_age)
        n_sessions += 1
        t = lo + max_age
        while t <= hi:
            store = hist.reconstruct(t)
            store.joint_fit()
            if store._clock and store._clock.get("consts"):
                cc.observe(store._clock["consts"], t, ppm=ppm)
                frames += 1
            t += _STEP_S
    cc.flush()
    return {"sessions": n_sessions, "frames": frames, "aircraft": len(cc._ac)}
