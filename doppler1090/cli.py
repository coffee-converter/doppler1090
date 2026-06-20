import argparse
import numpy as np
from rich.live import Live
from .constants import DEFAULT_FS, DEFAULT_FREQ
from .capture import iq_chunks
from .detect import magnitude, detect_preambles
from .decode import demodulate, decode
from .estimate import estimate_burst_offset
from .decode import _chip_indices, bits_to_hex
from .track import TrackStore
from .terminal import build_rows, build_table

BURST_US = 120.0


def _phase_offsets(n):
    """Yield sample shifts 0, -1, +1, -2, +2, ... up to ±n for phase search."""
    yield 0
    for d in range(1, n + 1):
        yield -d
        yield d


def process_chunk(t, iq, fs, rx_llh, store, burst_len, max_fix=1, phase_search=1,
                  threshold=2.0):
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


def main(argv=None):
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
    args = p.parse_args(argv)

    rx_llh = (args.lat, args.lon, args.alt)
    store = TrackStore()
    burst_len = int(round(BURST_US * args.fs / 1e6)) + 8
    max_fix = 2 if args.aggressive else 1
    # rich.Live with screen=True paints into the alternate screen buffer (like
    # top/htop): a fixed region redrawn in place each frame. This avoids both the
    # flicker of clear()/reprint and the header duplication that inline Live
    # causes when the table's height changes between frames.
    with Live(build_table([]), refresh_per_second=4, screen=True) as live:
        for t, iq in iq_chunks(args.freq, args.fs, args.gain, args.ppm):
            process_chunk(t, iq, args.fs, rx_llh, store, burst_len,
                          max_fix=max_fix, phase_search=args.phase_search,
                          threshold=args.threshold)
            live.update(build_table(build_rows(store, rx_llh)))


if __name__ == "__main__":
    main()
