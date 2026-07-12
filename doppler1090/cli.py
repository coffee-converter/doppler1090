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
from .history import Recorder, History
from .terminal import build_rows, build_table
from . import server

BURST_US = 120.0


def _phase_offsets(n):
    """Yield sample shifts 0, -1, +1, -2, +2, ... up to ±n for phase search."""
    yield 0
    for d in range(1, n + 1):
        yield -d
        yield d


def process_chunk(t, iq, fs, rx_llh, store, burst_len, max_fix=1, phase_search=1,
                  threshold=2.0):
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
        before = store.burst_count(icao)
        store.add_burst(icao, t, f_off, rx_llh)
        added += store.burst_count(icao) - before
    return added


def build_parser():
    p = argparse.ArgumentParser(prog="doppler1090")
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--alt", type=float, default=0.0)
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
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    rx_llh = (args.lat, args.lon, args.alt)
    burst_len = int(round(BURST_US * args.fs / 1e6)) + 8
    max_fix = 2 if args.aggressive else 1
    min_conf = 0.0 if args.show_all else args.min_confidence

    recorder = history = None
    if not args.no_record:
        path = os.path.join(args.data_dir,
                            time.strftime("session-%Y%m%d-%H%M%S.sqlite"))
        recorder = Recorder(path, rx_llh)
        history = History(path, max_age=args.max_age)
        print(f"recording session to {path}")
    store = TrackStore(max_age=args.max_age, recorder=recorder)

    if args.web:
        def capture_loop():
            for t, iq in iq_chunks(args.freq, args.fs, args.gain, args.ppm):
                with store.lock:
                    process_chunk(t, iq, args.fs, rx_llh, store, burst_len,
                                  max_fix=max_fix, phase_search=args.phase_search,
                                  threshold=args.threshold)
                    if recorder:
                        recorder.flush()
        threading.Thread(target=capture_loop, daemon=True).start()
        server.serve(store, rx_llh, store.lock, min_conf, args.port,
                     open_browser=True, history=history)
        return

    # rich.Live with screen=True paints into the alternate screen buffer (like
    # top/htop): a fixed region redrawn in place each frame. This avoids both the
    # flicker of clear()/reprint and the header duplication that inline Live
    # causes when the table's height changes between frames.
    with Live(build_table([]), refresh_per_second=4, screen=True) as live:
        for t, iq in iq_chunks(args.freq, args.fs, args.gain, args.ppm):
            process_chunk(t, iq, args.fs, rx_llh, store, burst_len,
                          max_fix=max_fix, phase_search=args.phase_search,
                          threshold=args.threshold)
            if recorder:
                recorder.flush()
            live.update(build_table(build_rows(store, rx_llh, min_conf=min_conf)))


if __name__ == "__main__":
    main()
