import numpy as np
from .constants import F0_HZ, C_M_S

WGS84_A = 6378137.0
WGS84_E2 = 6.69437999014e-3
FT_TO_M = 0.3048    # ADS-B altitude is feet; geodetic_to_ecef wants metres


def geodetic_to_ecef(lat_deg, lon_deg, alt_m):
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)
    n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * np.sin(lat) ** 2)
    x = (n + alt_m) * np.cos(lat) * np.cos(lon)
    y = (n + alt_m) * np.cos(lat) * np.sin(lon)
    z = (n * (1.0 - WGS84_E2) + alt_m) * np.sin(lat)
    return np.array([x, y, z])


def enu_velocity_to_ecef(lat_deg, lon_deg, ve, vn, vu):
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)
    sl, cl = np.sin(lat), np.cos(lat)
    so, co = np.sin(lon), np.cos(lon)
    rot = np.array([
        [-so, -sl * co, cl * co],
        [co,  -sl * so, cl * so],
        [0.0,       cl,      sl],
    ])
    return rot @ np.array([ve, vn, vu])


def radial_velocity(rx_llh, ac_llh, speed_mps, track_deg, vrate_mps):
    rx = geodetic_to_ecef(*rx_llh)
    ac = geodetic_to_ecef(*ac_llh)
    los = ac - rx
    los_hat = los / np.linalg.norm(los)
    track = np.radians(track_deg)
    ve = speed_mps * np.sin(track)
    vn = speed_mps * np.cos(track)
    v_ecef = enu_velocity_to_ecef(ac_llh[0], ac_llh[1], ve, vn, vrate_mps)
    return float(np.dot(v_ecef, los_hat))  # positive = receding


def predicted_doppler(v_radial_mps):
    return -F0_HZ * v_radial_mps / C_M_S
