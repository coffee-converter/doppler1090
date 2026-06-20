from doppler1090 import constants


def test_constants_values():
    assert constants.F0_HZ == 1090e6
    assert constants.C_M_S == 299792458.0
    assert constants.DEFAULT_FS == 2_000_000
    assert constants.DEFAULT_FREQ == 1_090_000_000
