import bisect
import numpy as np

PREAMBLE_PULSES_US = (0.0, 1.0, 3.5, 4.5)
PREAMBLE_LOW_US = (0.5, 1.5, 2.5, 4.0, 5.0)
PREAMBLE_LEN_US = 8.0


def magnitude(iq):
    return np.abs(np.asarray(iq))


def detect_preambles(mag, fs, threshold_factor=3.0):
    mag = np.asarray(mag)
    spb = fs / 1e6
    pulse_idx = np.round(np.array(PREAMBLE_PULSES_US) * spb).astype(int)
    low_idx = np.round(np.array(PREAMBLE_LOW_US) * spb).astype(int)
    win = int(round(PREAMBLE_LEN_US * spb))
    valid = len(mag) - win
    if valid <= 0:
        return []
    base = np.arange(valid)
    high = mag[base[:, None] + pulse_idx[None, :]].sum(axis=1)
    low = mag[base[:, None] + low_idx[None, :]].sum(axis=1) + 1e-9
    cand = np.where(high > threshold_factor * low)[0]
    # Non-maximum suppression: take strongest candidates first, drop any within
    # one preamble window of an already-accepted (stronger) peak. Accepted peaks
    # are kept sorted, so each is pairwise >= win apart; therefore the closest
    # accepted peak to a new candidate is one of its two sorted neighbors, and we
    # only need to check those (O(N log N) via bisect, not O(N^2)).
    order = cand[np.argsort(-high[cand])]
    accepted = []  # kept sorted by position
    for c in order:
        c = int(c)
        pos = bisect.bisect_left(accepted, c)
        if pos < len(accepted) and accepted[pos] - c < win:
            continue
        if pos > 0 and c - accepted[pos - 1] < win:
            continue
        accepted.insert(pos, c)
    return accepted
