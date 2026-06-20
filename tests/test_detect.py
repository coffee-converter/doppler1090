import numpy as np
from doppler1090.detect import magnitude, detect_preambles


def _make_preamble(fs, offset, total, height=5.0, floor=0.2):
    mag = np.full(total, floor)
    spb = fs / 1e6
    for us in (0.0, 1.0, 3.5, 4.5):  # Mode S preamble pulse centers
        mag[offset + int(round(us * spb))] = height
    return mag


def test_magnitude():
    iq = np.array([3 + 4j, 0 + 0j])
    assert np.allclose(magnitude(iq), [5.0, 0.0])


def test_detects_preamble_at_offset():
    fs, offset, total = 2_400_000, 1000, 4000
    mag = _make_preamble(fs, offset, total)
    offsets = detect_preambles(mag, fs)
    assert any(abs(o - offset) <= 1 for o in offsets)


def test_no_false_positive_on_flat_noise():
    rng = np.random.default_rng(1)
    mag = 0.2 + 0.01 * rng.standard_normal(4000)
    assert detect_preambles(mag, 2_400_000) == []
