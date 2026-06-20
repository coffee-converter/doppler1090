import numpy as np
from doppler1090.cli import process_chunk
from doppler1090.track import TrackStore


def _synth_burst(hexstr, fs, f_off=0.0, height=4.0):
    bits = bin(int(hexstr, 16))[2:].zfill(len(hexstr) * 4)
    spb = fs / 1e6
    total = int(round((8.0 + 112.0) * spb)) + 8
    mag = np.zeros(total)
    for us in (0.0, 1.0, 3.5, 4.5):  # preamble pulses
        mag[int(round(us * spb))] = height
    start = int(round(8.0 * spb))
    for k, b in enumerate(bits):
        idx = start + int(round((k + (0 if b == "1" else 0.5)) * spb))
        mag[idx] = height
    t = np.arange(total) / fs
    return mag * np.exp(2j * np.pi * f_off * t)


def test_process_chunk_decodes_real_position_into_store():
    # Full wiring path on a real, CRC-valid position frame:
    # detect -> demodulate -> decode (real CRC) -> update_position.
    fs = 2_400_000
    store = TrackStore()
    rx = (52.0, 4.0, 0.0)
    pos = _synth_burst("8D40621D58C382D690C8AC2863A7", fs, f_off=1500.0)
    pad = np.zeros(64, dtype=complex)
    process_chunk(0.0, np.concatenate([pos, pad]), fs, rx, store, burst_len=len(pos))
    state = store.latest("40621D")
    assert "lat" in state
    assert abs(state["lat"] - 52.2572) < 0.01
    assert abs(state["lon"] - 3.9193) < 0.01


def test_process_chunk_adds_burst_with_estimated_offset():
    # Full Doppler path: position THEN velocity for the same ICAO. Once both are
    # present, a burst is deposited with the carrier offset estimated and a
    # predicted Doppler computed from geometry.
    fs = 2_400_000
    store = TrackStore()
    rx = (52.0, 4.0, 0.0)
    pos = _synth_burst("8D40621D58C382D690C8AC2863A7", fs, f_off=1500.0)
    vel = _synth_burst("8D40621D994409940838174550B1", fs, f_off=1500.0)
    pad = np.zeros(64, dtype=complex)
    process_chunk(0.0, np.concatenate([pos, pad]), fs, rx, store, burst_len=len(pos))
    added = process_chunk(1.0, np.concatenate([vel, pad]), fs, rx, store, burst_len=len(vel))
    assert added == 1
    assert store.burst_count("40621D") == 1
    sample = store._samples["40621D"][-1]
    assert abs(sample.f_offset - 1500.0) < 50.0   # carrier estimate recovered the injected offset
    assert np.isfinite(sample.doppler_pred)        # predicted Doppler computed from geometry
