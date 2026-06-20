import time
import ctypes
import numpy as np


def _load_rtlsdr():
    """Import pyrtlsdr's RtlSdr, tolerating librtlsdr builds that don't export
    every optional symbol.

    pyrtlsdr 0.4.0 binds functions like rtlsdr_set_dithering and the GPIO
    helpers eagerly at import time, but neither the osmocom mainline nor the
    rtl-sdr-blog fork of librtlsdr exports all of them. This tool never calls
    those functions, so we install a missing-symbol stub on ctypes during the
    import only, then restore the original behaviour.
    """
    original = ctypes.CDLL.__getitem__

    def tolerant(self, name):
        try:
            return original(self, name)
        except AttributeError:
            if isinstance(name, str) and name.startswith("rtlsdr_"):
                def _stub(*args, **kwargs):
                    return 0  # success; never actually called by this tool
                return _stub
            raise

    ctypes.CDLL.__getitem__ = tolerant
    try:
        from rtlsdr import RtlSdr  # PyPI package "pyrtlsdr" imports as "rtlsdr"
    finally:
        ctypes.CDLL.__getitem__ = original
    return RtlSdr


def iq_chunks(freq, fs, gain, ppm, chunk_size=262144, sdr_factory=None):
    if sdr_factory is None:
        try:
            sdr_factory = _load_rtlsdr()
        except (ImportError, OSError) as e:
            raise SystemExit(
                "Could not load the RTL-SDR driver. Install librtlsdr (the "
                "rtl-sdr-blog fork for V4 hardware) and the pyrtlsdr package.\n"
                f"Original error: {e}"
            )
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
