import numpy as np
from doppler1090.cli import process_chunk, build_parser
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


def test_parser_has_web_flags():
    p = build_parser()
    args = p.parse_args(["--lat", "1.0", "--lon", "2.0", "--web", "--port", "9999"])
    assert args.web is True
    assert args.port == 9999


def test_parser_web_defaults_off():
    p = build_parser()
    args = p.parse_args(["--lat", "1.0", "--lon", "2.0"])
    assert args.web is False
    assert args.port == 8080


def test_parser_max_age_default_and_override():
    p = build_parser()
    assert p.parse_args(["--lat", "1.0", "--lon", "2.0"]).max_age == 60.0
    assert p.parse_args(["--lat", "1.0", "--lon", "2.0", "--max-age", "30"]).max_age == 30.0


def test_process_chunk_prunes_stale_aircraft():
    fs = 2_400_000
    store = TrackStore(max_age=60.0)
    # an aircraft heard at t=0
    store.update_position("OLD123", 0.0, 1.0, 2.0, 10000.0)
    store._inject("OLD123", 0.0, 100.0, 50.0, 1.0, 2.0, 90.0)
    assert "OLD123" in store.icaos()
    # a later chunk with no signal (zeros) at t=100 -> prune runs, drops OLD123
    iq = np.zeros(4096, dtype=complex)
    process_chunk(100.0, iq, fs, (42.0, -88.0, 0.0), store, burst_len=300)
    assert "OLD123" not in store.icaos()


def test_min_confidence_defaults_to_zero_show_all():
    p = build_parser()
    args = p.parse_args(["--lat", "1.0", "--lon", "2.0"])
    assert args.min_confidence == 0.0          # default shows all decoded aircraft
    args2 = p.parse_args(["--lat", "1.0", "--lon", "2.0", "--min-confidence", "0.3"])
    assert args2.min_confidence == 0.3         # still tunable to filter
