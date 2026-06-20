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


def estimate_burst_offset(iq, mask, fs):
    iq = np.asarray(iq)
    mask = np.asarray(mask)
    # Extract only the samples where mask is nonzero ("on" chips)
    non_zero_indices = np.where(mask != 0)[0]
    extracted_iq = iq[non_zero_indices]

    # Compute effective sample rate based on spacing
    if len(non_zero_indices) > 0:
        # If mask is uniform with period P, effective fs is fs/P
        # For a binary mask, estimate P as average spacing
        if len(non_zero_indices) > 1:
            spacings = np.diff(non_zero_indices)
            avg_spacing = np.mean(spacings)
        else:
            avg_spacing = 1.0
        effective_fs = fs / avg_spacing
    else:
        effective_fs = fs

    return estimate_tone_freq(extracted_iq, effective_fs)
