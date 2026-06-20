import numpy as np
from doppler1090.decode import bits_to_hex, demodulate, decode


def test_bits_to_hex():
    bits = [1, 0, 0, 0, 1, 1, 0, 1]  # 0x8D
    assert bits_to_hex(bits) == "8D"


def _synth_burst(hexstr, fs, f_off=0.0, height=4.0):
    bits = bin(int(hexstr, 16))[2:].zfill(len(hexstr) * 4)
    spb = fs / 1e6
    total = int(round((8.0 + 112.0) * spb)) + 4
    mag = np.zeros(total)
    start = int(round(8.0 * spb))
    for k, b in enumerate(bits):
        c0 = start + int(round(k * spb))
        c1 = start + int(round((k + 0.5) * spb))
        if b == "1":
            mag[c0] = height
        else:
            mag[c1] = height
    t = np.arange(total) / fs
    return mag * np.exp(2j * np.pi * f_off * t)


def test_demodulate_roundtrip():
    fs = 2_400_000
    msg = "8D40621D58C382D690C8AC2863A7"
    iq = _synth_burst(msg, fs)
    assert demodulate(iq, fs) == msg


def test_decode_position_message():
    out = decode("8D40621D58C382D690C8AC2863A7", 52.0, 4.0)
    assert out["icao"].lower() == "40621d"
    assert abs(out["lat"] - 52.2572) < 0.01
    assert abs(out["lon"] - 3.9193) < 0.01


def test_decode_velocity_message():
    out = decode("8D485020994409940838175B284F", 52.0, 4.0)
    assert abs(out["speed"] - 159.0) < 2.0


def test_decode_rejects_non_df17():
    # DF11 all-call reply, not an extended squitter
    assert decode("5D484FDEA248F5", 52.0, 4.0) is None


def test_decode_rejects_full_length_non_df17():
    # 28-char frame that decodes as DF20 (not DF17) -> rejected by the df check.
    assert decode("A040621D58C382D690C8AC2863A7", 52.0, 4.0) is None


def _flip_hex_bit(hexstr, i):
    b = list(bin(int(hexstr, 16))[2:].zfill(112))
    b[i] = "1" if b[i] == "0" else "0"
    return "%028X" % int("".join(b), 2)


def test_decode_repairs_single_bit_error_by_default():
    # A real position frame with one corrupted data bit decodes correctly
    # because single-bit correction is on by default.
    bad = _flip_hex_bit("8D40621D58C382D690C8AC2863A7", 60)
    out = decode(bad, 52.0, 4.0)
    assert out is not None
    assert out["icao"] == "40621D"
    assert out["errorbits"] == 1
    assert abs(out["lat"] - 52.2572) < 0.01


def test_decode_does_not_repair_when_max_fix_zero():
    bad = _flip_hex_bit("8D40621D58C382D690C8AC2863A7", 60)
    assert decode(bad, 52.0, 4.0, max_fix=0) is None


def test_decode_repairs_two_bit_error_only_in_aggressive():
    bad = _flip_hex_bit(_flip_hex_bit("8D40621D58C382D690C8AC2863A7", 60), 90)
    assert decode(bad, 52.0, 4.0, max_fix=1) is None
    out = decode(bad, 52.0, 4.0, max_fix=2)
    assert out is not None and out["errorbits"] == 2


def test_decode_extracts_callsign_from_identification():
    out = decode("8D4840D6202CC371C32CE0576098", 52.0, 4.0)
    assert out is not None
    assert out["flight"] == "KLM1023"
    assert "lat" not in out and "speed" not in out  # ident carries neither
