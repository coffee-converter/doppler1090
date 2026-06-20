import numpy as np


def estimate_tone_freq(iq: np.ndarray, fs: float, oversample: int = 8) -> float:
    iq = np.asarray(iq)
    n = len(iq)
    win = np.hanning(n)
    nfft = oversample * n
    spec = np.fft.fftshift(np.fft.fft(iq * win, n=nfft))
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / fs))
    mag = np.abs(spec)
    k = int(np.argmax(mag))
    delta = 0.0
    if 0 < k < len(mag) - 1:
        a, b, c = mag[k - 1], mag[k], mag[k + 1]
        denom = a - 2.0 * b + c
        if denom < 0.0:          # only refine at a concave-down peak
            delta = 0.5 * (a - c) / denom
    df = freqs[1] - freqs[0]
    return float(freqs[k] + delta * df)
