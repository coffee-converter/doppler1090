import argparse
import os
import threading
import time
import numpy as np
from rich.live import Live
from .constants import DEFAULT_FS, DEFAULT_FREQ
from .capture import iq_chunks
from .detect import magnitude, detect_preambles
from .decode import demodulate, decode
from .estimate import estimate_burst_offset
from .decode import _chip_indices, bits_to_hex
from .track import TrackStore
from .geometry import FT_TO_M
from .history import Recorder, History, read_session_meta
from . import aircraft
from .records import Records
from .coverage import Coverage
from .clockcal import ClockCal, clock_estimate
from .terminal import build_rows, build_display
from . import server

BURST_US = 120.0


def _phase_offsets(n):
    """Yield sample shifts 0, -1, +1, -2, +2, ... up to ±n for phase search."""
    yield 0
    for d in range(1, n + 1):
        yield -d
        yield d


def process_chunk(t, iq, fs, rx_llh, store, burst_len, max_fix=1, phase_search=1,
                  threshold=2.0, health=None):
    store.prune(t)  # drop aircraft not heard from within max_age
    mag = magnitude(iq)
    offs = detect_preambles(mag, fs, threshold_factor=threshold)
    if not offs:
        return 0

    # Batch-demodulate every candidate at once: build one (N, 112) bit matrix by
    # comparing the two half-bit chips, instead of a Python call per candidate.
    c0, c1 = _chip_indices(fs)
    offs = np.array(offs)
    offs = offs[offs + c1[-1] < len(mag)]
    if offs.size == 0:
        return 0
    base = offs[:, None]
    bits = (mag[base + c0[None, :]] > mag[base + c1[None, :]]).astype(int)

    # Keep only candidates whose DF field (first 5 bits) is 17 - vectorized, so
    # the expensive per-frame work (CRC correction + pyModeS) runs on the handful
    # of real ADS-B candidates rather than on every noise spike.
    df = bits[:, :5] @ np.array([16, 8, 4, 2, 1])
    sel = np.nonzero(df == 17)[0]

    added = 0
    for i in sel:
        try:
            off = int(offs[i])
            # Try the primary alignment, then a small phase search around it.
            dec = decode(bits_to_hex(bits[i]), rx_llh[0], rx_llh[1], max_fix=max_fix)
            o = off
            if dec is None and phase_search:
                for d in _phase_offsets(phase_search):
                    if d == 0:
                        continue
                    oo = off + d
                    if oo < 0 or oo + burst_len > len(iq):
                        continue
                    dec = decode(demodulate(iq[oo:oo + burst_len], fs),
                                 rx_llh[0], rx_llh[1], max_fix=max_fix)
                    if dec is not None:
                        o = oo
                        break
            if dec is None:
                continue
            if health is not None:
                health.mark_decode()        # a real ADS-B frame got through
            sl = iq[o:o + burst_len]
            msl = mag[o:o + burst_len]
            icao = dec["icao"]
            if "flight" in dec:
                store.update_callsign(icao, dec["flight"], t)
            if "lat" in dec:
                store.update_position(icao, t, dec["lat"], dec["lon"], dec["alt"])
            if "speed" in dec:
                store.update_velocity(icao, t, dec["speed"], dec["track"], dec["vrate"])
            mask = (msl > msl.mean()).astype(float)
            f_off = estimate_burst_offset(sl, mask, fs)
            # signal strength: RMS amplitude of the message's ON pulses (0..~1.4 of
            # full scale); build_snapshot turns the recent average into dBFS.
            on = msl[msl > msl.mean()]
            sig = float(np.sqrt(np.mean(on * on))) if on.size else 0.0
            before = store.burst_count(icao)
            store.add_burst(icao, t, f_off, rx_llh, signal=sig)
            added += store.burst_count(icao) - before
        except Exception:
            # one malformed frame must not tear down the whole chunk - the
            # capture loop would misread that as an SDR fault and reopen the dongle
            continue
    return added


class _BurstRate:
    """Rolling bursts-per-minute over a short window, for the status header."""

    def __init__(self, window=60.0):
        self.window = window
        self._events = []

    def add(self, t, count):
        if count:
            self._events.append((t, count))
        cutoff = t - self.window
        self._events = [(tt, c) for tt, c in self._events if tt >= cutoff]

    def per_min(self, now):
        if not self._events:
            return 0.0
        total = sum(c for _, c in self._events)
        elapsed = max(now - self._events[0][0], 5.0)   # avoid early spikes
        return total * 60.0 / elapsed


def _live_status(rx_llh, health, rate, n_aircraft, now):
    """The terminal status-header dict for the current frame."""
    return {"rx": (rx_llh[0], rx_llh[1]),
            "uptime_s": now - health.started,
            "n_aircraft": n_aircraft,
            "burst_rate": rate.per_min(now),
            "sdr_state": health.snapshot(now)["state"],
            "error": health.error}


def build_parser():
    p = argparse.ArgumentParser(prog="doppler1090")
    p.add_argument("--lat", type=float,
                   help="receiver latitude (required unless --replay)")
    p.add_argument("--lon", type=float,
                   help="receiver longitude (required unless --replay)")
    p.add_argument("--alt", type=float, default=0.0,
                   help="receiver antenna elevation in feet (default 0)")
    p.add_argument("--gain", type=float, default=40.0)
    p.add_argument("--ppm", type=int, default=0)
    p.add_argument("--freq", type=int, default=DEFAULT_FREQ)
    p.add_argument("--fs", type=int, default=DEFAULT_FS)
    p.add_argument("--aggressive", action="store_true",
                   help="enable two-bit CRC error correction (more decodes, "
                        "but a higher chance of false repairs)")
    p.add_argument("--phase-search", type=int, default=1,
                   help="sample-offset phase search radius (0 disables)")
    p.add_argument("--threshold", type=float, default=2.0,
                   help="preamble detection threshold (lower = more sensitive, "
                        "more CPU; 2.0 recovers ~70%% more frames than 3.0)")
    p.add_argument("--min-confidence", type=float, default=0.0,
                   help="hide aircraft whose Doppler-fit confidence is below "
                        "this (0..1); default 0 (show all decoded aircraft, "
                        "dump1090-style). Raise it to surface only trustworthy "
                        "Doppler tracks.")
    p.add_argument("--show-all", action="store_true",
                   help="show every tracked aircraft regardless of confidence")
    p.add_argument("--max-age", type=float, default=60.0,
                   help="drop aircraft not heard from for this many seconds "
                        "(default 60)")
    p.add_argument("--web", action="store_true",
                   help="serve the browser dashboard instead of the terminal table")
    p.add_argument("--port", type=int, default=8080,
                   help="web dashboard port (default 8080)")
    p.add_argument("--data-dir", default="doppler1090-data",
                   help="directory for recorded session files (default "
                        "./doppler1090-data)")
    p.add_argument("--no-record", action="store_true",
                   help="do not persist the session; disables the web "
                        "dashboard's time-travel scrubber")
    p.add_argument("--no-lookup", action="store_true",
                   help="do not look up aircraft make/model (adsbdb.com)")
    p.add_argument("--faa-registry", action="store_true",
                   help="also fall back to the FAA registry for make/model "
                        "(covers US private/GA aircraft; ~73 MB one-time "
                        "download into --data-dir)")
    p.add_argument("--replay", metavar="FILE",
                   help="replay a recorded session file instead of capturing "
                        "live - no SDR needed. The receiver location and ppm "
                        "are read from the file, so --lat/--lon are not needed. "
                        "Works with --web (dashboard) or the terminal table.")
    p.add_argument("--replay-speed", type=float, default=1.0,
                   help="replay playback rate (default 1.0 = real time; "
                        "2.0 = twice as fast)")
    p.add_argument("--demo", action="store_true",
                   help="replay the bundled sample session - a self-contained "
                        "way to try doppler1090 with no SDR; add --web for the "
                        "browser dashboard")
    return p


def _bundled_sample():
    """Filesystem path to the sample session shipped inside the package."""
    from importlib.resources import files
    return str(files("doppler1090") / "examples" / "sample-session.sqlite")


def _run_replay(args):
    """Play back a recorded session file - no SDR, no capture thread. Receiver
    location and ppm come from the file, so the same physics and rendering run
    on reconstructed state exactly as they did live."""
    if not os.path.exists(args.replay):
        raise SystemExit(f"doppler1090: replay file not found: {args.replay}")
    rx_llh, ppm = read_session_meta(args.replay)
    min_conf = 0.0 if args.show_all else args.min_confidence
    history = History(args.replay, max_age=args.max_age)
    t_start, t_end = history.bounds()
    if t_start is None:
        raise SystemExit(f"doppler1090: {args.replay} has no data to replay")
    speed = args.replay_speed if args.replay_speed > 0 else 1.0
    print(f"replaying {args.replay}: {t_end - t_start:.0f}s at {speed:g}x "
          f"(rx {rx_llh[0]:.4f},{rx_llh[1]:.4f})")
    if args.web:
        return _replay_web(args, history, rx_llh, min_conf, ppm, t_start, t_end,
                           speed)
    _replay_terminal(history, rx_llh, min_conf, t_start, t_end, speed)


def _replay_terminal(history, rx_llh, min_conf, t_start, t_end, speed):
    """Terminal replay: a virtual clock sweeps the session at ``speed`` and the
    table is re-rendered from reconstructed state, looping at the end."""
    with Live(build_display([]), refresh_per_second=4, screen=True) as live:
        while True:
            wall0 = time.monotonic()
            at = t_start
            while at < t_end:
                store = history.reconstruct(at)
                rows = build_rows(store, rx_llh, min_conf=min_conf)
                status = {"rx": (rx_llh[0], rx_llh[1]),
                          "uptime_s": at - t_start,
                          "n_aircraft": len(rows),
                          "sdr_state": "replay"}
                live.update(build_display(rows, status, None,
                                          live.console.size.width))
                time.sleep(0.25)
                at = t_start + (time.monotonic() - wall0) * speed


def _replay_web(args, history, rx_llh, min_conf, ppm, t_start, t_end, speed):
    """Web replay: serve the dashboard against the file. The live store stays
    empty - the browser's scrubber drives reconstruction over [t_start, t_end]
    and loops. Enrichment stores are wired so make/model/photos still resolve."""
    os.makedirs(args.data_dir, exist_ok=True)
    store = TrackStore(max_age=args.max_age)
    type_store = None
    if not args.no_lookup:
        type_store = aircraft.TypeStore(
            os.path.join(args.data_dir, "aircraft.sqlite"),
            use_faa=args.faa_registry)
    records = Records(os.path.join(args.data_dir, "records.sqlite"))
    coverage = Coverage(os.path.join(args.data_dir, "coverage.sqlite"))
    clockcal = ClockCal(os.path.join(args.data_dir, "clockcal.sqlite"), ppm=ppm)
    server.serve(store, rx_llh, store.lock, min_conf, args.port,
                 open_browser=True, history=history, type_store=type_store,
                 health=None, records=records, coverage=coverage,
                 clockcal=clockcal,
                 replay={"t_start": t_start, "t_end": t_end, "speed": speed})


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.demo:
        args.replay = _bundled_sample()
    if args.replay:
        return _run_replay(args)
    if args.lat is None or args.lon is None:
        raise SystemExit("doppler1090: --lat and --lon are required "
                         "(or use --replay FILE to play back a recorded "
                         "session)")

    rx_llh = (args.lat, args.lon, args.alt * FT_TO_M)   # --alt is feet; geometry wants metres
    burst_len = int(round(BURST_US * args.fs / 1e6)) + 8
    max_fix = 2 if args.aggressive else 1
    min_conf = 0.0 if args.show_all else args.min_confidence

    recorder = history = None
    if not args.no_record:
        path = os.path.join(args.data_dir,
                            time.strftime("session-%Y%m%d-%H%M%S.sqlite"))
        recorder = Recorder(path, rx_llh, ppm=args.ppm)
        history = History(path, max_age=args.max_age)
        print(f"recording session to {path}")
    store = TrackStore(max_age=args.max_age, recorder=recorder)

    if args.web:
        health = server.Health()
        def capture_loop():
            # Reconnect loop: if the dongle is unplugged mid-stream iq_chunks
            # raises and closes the device; we flag the error (red light), wait,
            # and reopen - so plugging it back in recovers on its own.
            while True:
                try:
                    for t, iq in iq_chunks(args.freq, args.fs, args.gain, args.ppm):
                        health.mark_chunk()
                        if health.error:
                            print("doppler1090: SDR reconnected")
                            health.error = None
                        with store.lock:
                            process_chunk(t, iq, args.fs, rx_llh, store, burst_len,
                                          max_fix=max_fix, phase_search=args.phase_search,
                                          threshold=args.threshold, health=health)
                            if recorder:
                                recorder.flush()
                except Exception as e:  # dongle unplugged / driver error -> go red
                    health.error = str(e)
                    print(f"doppler1090: SDR capture error ({e}); retrying in 2s")
                time.sleep(2)           # wait before trying to reopen the dongle
        type_store = None
        os.makedirs(args.data_dir, exist_ok=True)
        if not args.no_lookup:
            type_store = aircraft.TypeStore(
                os.path.join(args.data_dir, "aircraft.sqlite"),
                use_faa=args.faa_registry)
        records = Records(os.path.join(args.data_dir, "records.sqlite"))
        coverage = Coverage(os.path.join(args.data_dir, "coverage.sqlite"))
        clockcal = ClockCal(os.path.join(args.data_dir, "clockcal.sqlite"),
                            ppm=args.ppm)
        threading.Thread(target=capture_loop, daemon=True).start()
        server.serve(store, rx_llh, store.lock, min_conf, args.port,
                     open_browser=True, history=history, type_store=type_store,
                     health=health, records=records, coverage=coverage,
                     clockcal=clockcal)
        return

    # rich.Live with screen=True paints into the alternate screen buffer (like
    # top/htop): a fixed region redrawn in place each frame. This avoids both the
    # flicker of clear()/reprint and the header duplication that inline Live
    # causes when the table's height changes between frames.
    # Enrichment stores mirror the web path: cached make/model lookups and the
    # ppm self-calibration, both surfaced in the header.
    os.makedirs(args.data_dir, exist_ok=True)
    type_store = None
    if not args.no_lookup:
        type_store = aircraft.TypeStore(
            os.path.join(args.data_dir, "aircraft.sqlite"),
            use_faa=args.faa_registry)
    clockcal = ClockCal(os.path.join(args.data_dir, "clockcal.sqlite"),
                        ppm=args.ppm)
    records = Records(os.path.join(args.data_dir, "records.sqlite"))
    health = server.Health()
    rate = _BurstRate()
    # Reconnect loop mirrors the web path: if the dongle can't be opened (not
    # plugged in) or drops mid-stream, iq_chunks raises; instead of crashing we
    # flag the error in the status header (red light) and retry, so plugging the
    # SDR in recovers on its own.
    with Live(build_display([]), refresh_per_second=4, screen=True) as live:
        while True:
            try:
                for t, iq in iq_chunks(args.freq, args.fs, args.gain, args.ppm):
                    health.mark_chunk()
                    health.error = None           # data is flowing again
                    added = process_chunk(t, iq, args.fs, rx_llh, store, burst_len,
                                          max_fix=max_fix,
                                          phase_search=args.phase_search,
                                          threshold=args.threshold)
                    if store.icaos():             # tracking anything -> light green
                        health.mark_decode()
                    if recorder:
                        recorder.flush()
                    now = time.time()
                    rate.add(now, added)
                    rows = build_rows(store, rx_llh, min_conf=min_conf,
                                      type_store=type_store)
                    clock = clock_estimate(store, clockcal, now)
                    for r in rows:
                        records.observe({"icao": r.icao, "flight": r.flight,
                                         "make": r.make, "model": r.model,
                                         "reg": r.reg, "speed_kt": r.speed_kt,
                                         "alt": r.alt_ft, "vrate": r.vrate_fpm,
                                         "range_km": r.range_km, "rssi": r.rssi,
                                         "dop_span": r.dop_span}, now)
                    live.update(build_display(
                        rows, _live_status(rx_llh, health, rate, len(rows), now),
                        clock, live.console.size.width,
                        records=records.snapshot()))
            except Exception as e:                # dongle missing / driver error
                health.error = str(e)
                live.update(build_display(
                    build_rows(store, rx_llh, min_conf=min_conf,
                               type_store=type_store),
                    _live_status(rx_llh, health, rate, 0, time.time()),
                    None, live.console.size.width))
            time.sleep(2)                         # wait before reopening the dongle


if __name__ == "__main__":
    main()
