import time
import numpy as np


def iq_chunks(freq, fs, gain, ppm, chunk_size=262144, sdr_factory=None):
    if sdr_factory is None:
        from pyrtlsdr import RtlSdr
        sdr_factory = RtlSdr
    sdr = sdr_factory()
    try:
        sdr.sample_rate = fs
        sdr.center_freq = freq
        sdr.gain = gain
        if ppm:
            sdr.freq_correction = int(ppm)
    except AttributeError:
        pass  # injected fakes may not implement config setters
    try:
        while True:
            samples = np.asarray(sdr.read_samples(chunk_size))
            yield (time.time(), samples)
    finally:
        sdr.close()
