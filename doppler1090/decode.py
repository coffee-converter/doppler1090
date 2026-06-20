import numpy as np
import pyModeS as pms


def bits_to_hex(bits):
    s = "".join(str(b) for b in bits)
    return "".join("%X" % int(s[i:i + 4], 2) for i in range(0, len(s), 4))


def demodulate(iq_slice, fs):
    mag = np.abs(np.asarray(iq_slice))
    spb = fs / 1e6
    start = int(round(8.0 * spb))
    bits = []
    for k in range(112):
        c0 = start + int(round(k * spb))
        c1 = start + int(round((k + 0.5) * spb))
        if c1 >= len(mag):
            return None
        bits.append(1 if mag[c0] > mag[c1] else 0)
    return bits_to_hex(bits)


def decode(hexstr, rx_lat, rx_lon):
    if not hexstr or len(hexstr) != 28:
        return None
    try:
        r = pms.decode(hexstr, reference=(rx_lat, rx_lon))
    except Exception:
        return None
    if r is None:
        return None
    if r.get("df") != 17 or not r.get("crc_valid"):
        return None
    out = {"icao": r["icao"], "tc": r.get("typecode")}
    if r.get("latitude") is not None:  # airborne position message
        out.update(lat=r["latitude"], lon=r["longitude"], alt=r.get("altitude"))
    if r.get("groundspeed") is not None:  # airborne velocity message
        out.update(speed=r["groundspeed"], track=r["track"], vrate=r["vertical_rate"])
    return out
