import functools
import numpy as np
import pyModeS as pms
from pyModeS import util
from .correct import fix_message

_HEX = np.array(list("0123456789ABCDEF"))


def bits_to_hex(bits):
    bits = np.asarray(bits, dtype=int).reshape(-1, 4)
    nibbles = bits[:, 0] * 8 + bits[:, 1] * 4 + bits[:, 2] * 2 + bits[:, 3]
    return "".join(_HEX[nibbles])


@functools.lru_cache(maxsize=8)
def _chip_indices(fs):
    """Sample indices of the two half-bit chips for each of the 112 data bits,
    cached per sample rate (they depend only on fs)."""
    spb = fs / 1e6
    start = int(round(8.0 * spb))
    k = np.arange(112)
    c0 = start + np.round(k * spb).astype(int)
    c1 = start + np.round((k + 0.5) * spb).astype(int)
    return c0, c1


def demodulate(iq_slice, fs):
    mag = np.abs(np.asarray(iq_slice))
    c0, c1 = _chip_indices(fs)
    if c1[-1] >= len(mag):
        return None
    bits = (mag[c0] > mag[c1]).astype(int)  # PPM: first half stronger -> 1
    return bits_to_hex(bits)


def decode(hexstr, rx_lat, rx_lon, max_fix=1):
    if not hexstr or len(hexstr) != 28:
        return None
    # The DF field (first 5 bits) is never error-corrected, so we can reject
    # non-DF17 candidates up front - this also avoids "repairing" noise into a
    # spurious DF17 frame.
    try:
        if util.df(hexstr) != 17:
            return None
    except Exception:
        return None
    corrected, nfix = fix_message(hexstr, max_bits=max_fix)
    if corrected is None:
        return None
    try:
        r = pms.decode(corrected, reference=(rx_lat, rx_lon))
    except Exception:
        return None
    if r is None:
        return None
    if r.get("df") != 17 or not r.get("crc_valid"):
        return None
    out = {"icao": r["icao"], "tc": r.get("typecode"), "errorbits": nfix}
    if r.get("callsign"):  # aircraft identification message (TC 1-4)
        out["flight"] = r["callsign"].strip().rstrip("_")
    if r.get("latitude") is not None:  # airborne position message
        out.update(lat=r["latitude"], lon=r["longitude"], alt=r.get("altitude"))
    if r.get("groundspeed") is not None:  # airborne velocity message
        out.update(speed=r["groundspeed"], track=r["track"], vrate=r["vertical_rate"])
    return out
