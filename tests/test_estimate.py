import numpy as np
from doppler1090.estimate import estimate_tone_freq, estimate_burst_offset


def test_recovers_clean_tone():
    fs, n, f_true = 2_400_000, 4096, 137_000.0
    t = np.arange(n) / fs
    iq = np.exp(2j * np.pi * f_true * t)
    assert abs(estimate_tone_freq(iq, fs) - f_true) < 50.0


def test_recovers_negative_tone_in_noise():
    rng = np.random.default_rng(0)
    fs, n, f_true = 2_400_000, 4096, -90_000.0
    t = np.arange(n) / fs
    iq = np.exp(2j * np.pi * f_true * t)
    iq = iq + 0.3 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    assert abs(estimate_tone_freq(iq, fs) - f_true) < 500.0


def test_burst_offset_with_gaps():
    fs, n, f_true = 2_400_000, 300, 200_000.0
    t = np.arange(n) / fs
    mask = np.zeros(n)
    mask[::2] = 1.0  # OOK-like on/off comb
    iq = np.exp(2j * np.pi * f_true * t)
    assert abs(estimate_burst_offset(iq, mask, fs) - f_true) < 2000.0
