import pyModeS as pms
from doppler1090.correct import fix_message, _flip

GOOD = "8D40621D58C382D690C8AC2863A7"  # real CRC-valid DF17 position frame


def test_valid_frame_passes_through_unchanged():
    assert fix_message(GOOD, max_bits=1) == (GOOD, 0)


def test_single_bit_error_is_repaired():
    bad = _flip(GOOD, 42)
    assert pms.util.crc(bad) != 0
    fixed, n = fix_message(bad, max_bits=1)
    assert fixed == GOOD
    assert n == 1


def test_two_bit_error_not_fixed_in_single_mode():
    bad = _flip(GOOD, 42, 77)
    assert fix_message(bad, max_bits=1) == (None, -1)


def test_two_bit_error_is_repaired_in_aggressive_mode():
    bad = _flip(GOOD, 42, 77)
    fixed, n = fix_message(bad, max_bits=2)
    assert fixed == GOOD
    assert n == 2


def test_max_bits_zero_validates_only():
    assert fix_message(_flip(GOOD, 42), max_bits=0) == (None, -1)
    assert fix_message(GOOD, max_bits=0) == (GOOD, 0)


def test_wrong_length_rejected():
    assert fix_message("8D4062", max_bits=2) == (None, -1)
    assert fix_message(None, max_bits=2) == (None, -1)
