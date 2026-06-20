import numpy as np
from dataclasses import dataclass
from .geometry import radial_velocity, predicted_doppler

KT_TO_MPS = 0.514444
FPM_TO_MPS = 0.00508
MIN_BURSTS = 8


@dataclass
class Sample:
    t: float
    f_offset: float
    doppler_pred: float
    lat: float
    lon: float
    track: float


@dataclass
class FitResult:
    scale: float
    correlation: float
    n: int
    quality: float
    measured_doppler: np.ndarray


class TrackStore:
    def __init__(self, max_age=300.0):
        self._samples = {}
        self._state = {}
        self.max_age = max_age

    def _st(self, icao):
        return self._state.setdefault(icao, {})

    def update_position(self, icao, t, lat, lon, alt):
        self._st(icao).update(lat=lat, lon=lon, alt=alt, t=t)

    def update_velocity(self, icao, t, speed_kt, track_deg, vrate_fpm):
        self._st(icao).update(
            speed=speed_kt * KT_TO_MPS,
            track=track_deg,
            vrate=vrate_fpm * FPM_TO_MPS,
            t=t,
        )

    def latest(self, icao):
        return dict(self._state.get(icao, {}))

    def _inject(self, icao, t, f_offset, doppler_pred, lat, lon, track):
        self._samples.setdefault(icao, []).append(
            Sample(t, f_offset, doppler_pred, lat, lon, track))

    def add_burst(self, icao, t, f_offset, rx_llh):
        s = self._state.get(icao, {})
        if not all(k in s for k in ("lat", "lon", "alt", "speed", "track", "vrate")):
            return
        vr = radial_velocity(rx_llh, (s["lat"], s["lon"], s["alt"]),
                             s["speed"], s["track"], s["vrate"])
        self._inject(icao, t, f_offset, predicted_doppler(vr),
                     s["lat"], s["lon"], s["track"])

    def icaos(self):
        return list(self._samples.keys())

    def burst_count(self, icao):
        return len(self._samples.get(icao, []))

    def quality(self, icao):
        s = self._samples.get(icao, [])
        if len(s) < MIN_BURSTS:
            return 0.0
        ang = np.radians([x.track for x in s])
        straightness = float(np.abs(np.mean(np.exp(1j * ang))))
        n_factor = min(1.0, len(s) / 40.0)
        return straightness * n_factor

    def fit(self, icao):
        s = self._samples.get(icao, [])
        if len(s) < MIN_BURSTS:
            return None
        t = np.array([x.t for x in s], dtype=float)
        t = t - t[0]
        f = np.array([x.f_offset for x in s])
        d = np.array([x.doppler_pred for x in s])
        design = np.column_stack([np.ones_like(t), t, d])
        coef, *_ = np.linalg.lstsq(design, f, rcond=None)
        b0, b1, scale = coef
        baseline = b0 + b1 * t
        measured = f - baseline
        if np.std(measured) > 0 and np.std(d) > 0:
            corr = float(np.corrcoef(measured, d)[0, 1])
        else:
            corr = 0.0
        return FitResult(float(scale), corr, len(s), self.quality(icao), measured)
