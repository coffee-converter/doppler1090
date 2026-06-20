import numpy as np
from doppler1090.geometry import (
    geodetic_to_ecef, radial_velocity, predicted_doppler,
)


def test_overhead_level_flight_has_near_zero_radial():
    rx = (0.0, 0.0, 0.0)
    ac = (0.0, 0.0, 10000.0)  # directly overhead
    vr = radial_velocity(rx, ac, speed_mps=250.0, track_deg=0.0, vrate_mps=0.0)
    assert abs(vr) < 1.0  # horizontal motion is perpendicular to the up LOS


def test_approaching_aircraft_is_blueshift():
    rx = (0.0, 0.0, 0.0)
    ac = (0.0, 0.1, 10000.0)        # east of receiver
    vr = radial_velocity(rx, ac, speed_mps=250.0, track_deg=270.0, vrate_mps=0.0)
    assert vr < 0.0                  # flying west = approaching = closing
    assert predicted_doppler(vr) > 0.0  # blueshift


def test_predicted_doppler_magnitude():
    # 250 m/s closing -> +908.6 Hz at 1090 MHz
    assert abs(predicted_doppler(-250.0) - 908.6) < 1.0


def test_ecef_equator_prime_meridian():
    xyz = geodetic_to_ecef(0.0, 0.0, 0.0)
    assert abs(xyz[0] - 6378137.0) < 1.0
    assert abs(xyz[1]) < 1e-6
    assert abs(xyz[2]) < 1e-6
