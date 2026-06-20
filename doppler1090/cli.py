import argparse
import numpy as np
from rich.live import Live
from .constants import DEFAULT_FS, DEFAULT_FREQ
from .capture import iq_chunks
from .detect import magnitude, detect_preambles
from .decode import demodulate, decode
from .estimate import estimate_burst_offset
from .track import TrackStore
from .terminal import build_rows, build_table

BURST_US = 120.0


def process_chunk(t, iq, fs, rx_llh, store, burst_len):
    mag = magnitude(iq)
    added = 0
    for off in detect_preambles(mag, fs):
        end = off + burst_len
        if end > len(iq):
            continue
        sl = iq[off:end]
        msl = mag[off:end]
        hexstr = demodulate(sl, fs)
        dec = decode(hexstr, rx_llh[0], rx_llh[1])
        if dec is None:
            continue
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
    args = p.parse_args(argv)

    rx_llh = (args.lat, args.lon, args.alt)
    store = TrackStore()
    burst_len = int(round(BURST_US * args.fs / 1e6)) + 8
    # rich.Live with screen=True paints into the alternate screen buffer (like
    # top/htop): a fixed region redrawn in place each frame. This avoids both the
    # flicker of clear()/reprint and the header duplication that inline Live
    # causes when the table's height changes between frames.
    with Live(build_table([]), refresh_per_second=4, screen=True) as live:
        for t, iq in iq_chunks(args.freq, args.fs, args.gain, args.ppm):
            process_chunk(t, iq, args.fs, rx_llh, store, burst_len)
            live.update(build_table(build_rows(store, rx_llh)))


if __name__ == "__main__":
    main()
