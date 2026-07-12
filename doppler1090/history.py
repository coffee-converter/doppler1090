"""Durable session recording and past-state reconstruction.

The live :class:`~doppler1090.track.TrackStore` is only a ~60 s window onto the
signal. To let the web dashboard scrub back through a whole session, every
decoded state message and every Doppler burst is appended to a per-session
SQLite file (:class:`Recorder`). :class:`History` reads that file back and
rebuilds a throwaway ``TrackStore`` *as of* any past instant, so the existing
``build_snapshot`` / ``joint_fit`` physics runs unchanged on reconstructed data
- a past frame is computed exactly the way the live frame was.

SQLite is used in WAL mode: the capture thread owns one writer connection while
request threads each open their own read-only connection, so scrubbing never
blocks live capture.
"""

import os
import sqlite3
import time

from .track import Sample, TrackStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY,
    started_at REAL,
    rx_lat     REAL,
    rx_lon     REAL,
    rx_alt     REAL
);
CREATE TABLE IF NOT EXISTS state_log (
    t         REAL,
    icao      TEXT,
    lat       REAL,
    lon       REAL,
    alt       REAL,
    speed_kt  REAL,
    track     REAL,
    vrate_fpm REAL,
    flight    TEXT
);
CREATE TABLE IF NOT EXISTS burst_log (
    t            REAL,
    icao         TEXT,
    f_offset     REAL,
    doppler_pred REAL,
    lat          REAL,
    lon          REAL,
    track        REAL
);
CREATE INDEX IF NOT EXISTS ix_state_icao_t ON state_log (icao, t);
CREATE INDEX IF NOT EXISTS ix_state_t      ON state_log (t);
CREATE INDEX IF NOT EXISTS ix_burst_icao_t ON burst_log (icao, t);
CREATE INDEX IF NOT EXISTS ix_burst_t      ON burst_log (t);
"""

# How far back a reconstruction query looks for an active aircraft's samples.
# Bounds per-frame query cost; comfortably longer than any single pass stays
# above the horizon, so a whole pass is still captured.
DEFAULT_LOOKBACK = 1800.0


class Recorder:
    """Append-only writer for one session. All calls happen on the capture
    thread, buffered per IQ chunk and flushed in a single transaction."""

    def __init__(self, path, rx_llh, started_at=None):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "INSERT INTO sessions (started_at, rx_lat, rx_lon, rx_alt) "
            "VALUES (?, ?, ?, ?)",
            (started_at if started_at is not None else time.time(),
             rx_llh[0], rx_llh[1], rx_llh[2]))
        self._conn.commit()
        self._states = []
        self._bursts = []

    def log_state(self, t, icao, *, lat=None, lon=None, alt=None,
                  speed_kt=None, track=None, vrate_fpm=None, flight=None):
        self._states.append((t, icao, lat, lon, alt, speed_kt, track,
                             vrate_fpm, flight))

    def log_burst(self, t, icao, f_offset, doppler_pred, lat, lon, track):
        self._bursts.append((t, icao, f_offset, doppler_pred, lat, lon, track))

    def flush(self):
        if not self._states and not self._bursts:
            return
        with self._conn:
            if self._states:
                self._conn.executemany(
                    "INSERT INTO state_log (t, icao, lat, lon, alt, speed_kt, "
                    "track, vrate_fpm, flight) VALUES (?,?,?,?,?,?,?,?,?)",
                    self._states)
            if self._bursts:
                self._conn.executemany(
                    "INSERT INTO burst_log (t, icao, f_offset, doppler_pred, "
                    "lat, lon, track) VALUES (?,?,?,?,?,?,?)",
                    self._bursts)
        self._states.clear()
        self._bursts.clear()

    def close(self):
        self.flush()
        self._conn.close()


# Fields carried in state_log, newest-non-null-wins when reconstructing.
_STATE_FIELDS = ("lat", "lon", "alt", "speed_kt", "track", "vrate_fpm", "flight")


class History:
    """Read side: rebuilds past state from a recorded session file."""

    def __init__(self, path, max_age, lookback=DEFAULT_LOOKBACK):
        self.path = path
        self.max_age = max_age
        self.lookback = lookback

    def _read(self):
        # Fresh read-only connection per request thread (WAL allows concurrent
        # readers alongside the single capture-thread writer).
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)

    def reconstruct(self, at):
        """Return a throwaway TrackStore holding state as of epoch ``at``,
        ready to hand to ``build_snapshot`` / ``joint_fit`` unchanged."""
        store = TrackStore(max_age=self.max_age)
        conn = self._read()
        try:
            built = []
            for icao in self._active_icaos(conn, at):
                samples = self._segment(conn, icao, at)
                if samples:
                    built.append((samples[0].t, icao, samples))
            # first-seen order (by earliest sample) so the list stays stable as
            # the playhead moves, matching the live view's insertion order.
            built.sort(key=lambda x: x[0])
            for _, icao, samples in built:
                store._samples[icao] = samples
                store._state[icao] = self._latest_state(conn, icao, at)
                store._last_seen[icao] = samples[-1].t
        finally:
            conn.close()
        return store

    def _active_icaos(self, conn, at):
        """Aircraft heard within max_age before ``at`` (mirrors prune)."""
        lo = at - self.max_age
        icaos = set()
        for table in ("burst_log", "state_log"):
            for (icao,) in conn.execute(
                    f"SELECT DISTINCT icao FROM {table} "
                    "WHERE t > ? AND t <= ?", (lo, at)):
                icaos.add(icao)
        return icaos

    def _segment(self, conn, icao, at):
        """The aircraft's burst run ending at/before ``at``, cut at the first
        gap longer than max_age (so a previous pass never pollutes the fit -
        exactly what live prune() does)."""
        rows = conn.execute(
            "SELECT t, f_offset, doppler_pred, lat, lon, track FROM burst_log "
            "WHERE icao = ? AND t > ? AND t <= ? ORDER BY t",
            (icao, at - self.lookback, at)).fetchall()
        if not rows:
            return []
        start = 0
        for i in range(1, len(rows)):
            if rows[i][0] - rows[i - 1][0] > self.max_age:
                start = i  # gap: everything before restarts a fresh pass
        return [Sample(t, f, d, lat, lon, trk)
                for (t, f, d, lat, lon, trk) in rows[start:]]

    def _latest_state(self, conn, icao, at):
        """Newest-non-null-wins across recent state_log rows - reproduces how
        the live store's per-field dict.update() accumulates."""
        state = {}
        for row in conn.execute(
                "SELECT lat, lon, alt, speed_kt, track, vrate_fpm, flight "
                "FROM state_log WHERE icao = ? AND t > ? AND t <= ? "
                "ORDER BY t DESC", (icao, at - self.lookback, at)):
            for key, val in zip(_STATE_FIELDS, row):
                if val is not None and key not in state:
                    state[key] = val
            if all(k in state for k in _STATE_FIELDS):
                break
        return state

    # "Nice" bucket widths (seconds). The strip keeps a *fixed* seconds-per-bar
    # so a burst always lands in the same bar regardless of how far `now` has
    # advanced - bars only ever get appended on the right, never re-bucketed.
    # Starts at 2 s (thin bars); steps up to the next nice value only once the
    # session outgrows ``max_buckets`` bars - rare, and a one-time re-layout.
    _NICE_WIDTHS = (2, 5, 10, 15, 30, 60, 120, 300, 600, 1800)

    def timeline(self, max_buckets=1800, now=None):
        """Session extent plus a per-bucket burst count for the activity strip.
        Buckets are a fixed width anchored at the session start, so the bars
        stay put as time advances. Returns {start, end, width, buckets:[int]}."""
        now = time.time() if now is None else now
        conn = self._read()
        try:
            row = conn.execute(
                "SELECT MIN(t), MAX(t) FROM ("
                "SELECT t FROM burst_log WHERE t > 0 "
                "UNION ALL SELECT t FROM state_log WHERE t > 0)"
            ).fetchone()
            lo, hi = row if row else (None, None)
            if lo is None:
                return {"start": now, "end": now, "width": self._NICE_WIDTHS[0],
                        "buckets": [0]}
            hi = max(hi, now)
            span = (hi - lo) or 1.0
            width = next((w for w in self._NICE_WIDTHS if span / w <= max_buckets),
                         self._NICE_WIDTHS[-1])
            nb = int(span // width) + 1
            buckets = [0] * nb
            for (t,) in conn.execute("SELECT t FROM burst_log WHERE t > 0"):
                buckets[min(int((t - lo) / width), nb - 1)] += 1
            # end is the right edge of the last (grid-aligned) bucket, so the
            # client maps bar positions on the same fixed grid.
            return {"start": lo, "end": lo + nb * width, "width": width,
                    "buckets": buckets}
        finally:
            conn.close()
