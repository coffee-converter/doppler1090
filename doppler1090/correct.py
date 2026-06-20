"""CRC-syndrome error correction for Mode S / ADS-B frames.

The 24-bit Mode S CRC is linear, so the syndrome of a received frame (its CRC
remainder) equals the XOR of the per-bit syndromes of whichever bits are in
error. We precompute the syndrome of every single-bit flip once, then:

  * single-bit table: syndrome -> bit index
  * two-bit table:    (syndrome_i XOR syndrome_j) -> (i, j)

On a frame whose CRC fails, we look its syndrome up and flip the offending
bit(s). This mirrors dump1090's fixSingleBitErrors / fixTwoBitsErrors. As in
dump1090, the first 5 bits (the DF field) are never corrected.

Two-bit correction is far more likely to "repair" random noise into a
plausible frame (5671 patterns vs 107 against a 24-bit CRC), so callers should
gate it (dump1090 only enables it in aggressive mode).
"""
import itertools
from pyModeS import util

_FRAME_BITS = 112
_FIRST_DATA_BIT = 5  # don't correct the DF field, matching dump1090


def _flip(hexstr, *bits):
    b = list(bin(int(hexstr, 16))[2:].zfill(_FRAME_BITS))
    for i in bits:
        b[i] = "1" if b[i] == "0" else "0"
    return "%028X" % int("".join(b), 2)


def _build_tables():
    zero = "0" * 28
    per_bit = {i: util.crc(_flip(zero, i)) for i in range(_FIRST_DATA_BIT, _FRAME_BITS)}
    single = {syn: i for i, syn in per_bit.items()}
    double = {}
    for i, j in itertools.combinations(range(_FIRST_DATA_BIT, _FRAME_BITS), 2):
        double[per_bit[i] ^ per_bit[j]] = (i, j)
    return single, double


_SINGLE, _DOUBLE = _build_tables()


def fix_message(hexstr, max_bits=1):
    """Repair a 28-hex-char (112-bit) Mode S frame using CRC syndrome lookup.

    Returns (corrected_hex, n_bits_fixed):
      * (hexstr, 0)            if the CRC is already valid
      * (corrected, 1 or 2)    if a 1- or 2-bit correction makes the CRC valid
      * (None, -1)             if it cannot be corrected within max_bits

    max_bits in {0, 1, 2}: 0 disables correction (validate only).
    """
    if not hexstr or len(hexstr) != 28:
        return None, -1
    syn = util.crc(hexstr)
    if syn == 0:
        return hexstr, 0
    if max_bits >= 1 and syn in _SINGLE:
        return _flip(hexstr, _SINGLE[syn]), 1
    if max_bits >= 2 and syn in _DOUBLE:
        i, j = _DOUBLE[syn]
        return _flip(hexstr, i, j), 2
    return None, -1
