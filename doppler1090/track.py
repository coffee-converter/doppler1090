import threading
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
    dop_span: float  # peak-to-peak predicted Doppler over the window (Hz)


class TrackStore:
    def __init__(self, max_age=300.0):
        self._samples = {}
        self._state = {}
        self.max_age = max_age
        self.lock = threading.Lock()  # held externally around mutation/snapshot

    def _st(self, icao):
        return self._state.setdefault(icao, {})

    def update_position(self, icao, t, lat, lon, alt):
        self._st(icao).update(lat=lat, lon=lon, alt=alt, t=t)

    def update_velocity(self, icao, t, speed_kt, track_deg, vrate_fpm):
        self._st(icao).update(
            speed=speed_kt * KT_TO_MPS,    # m/s, for the Doppler geometry
            track=track_deg,
            vrate=vrate_fpm * FPM_TO_MPS,  # m/s, for the Doppler geometry
            speed_kt=speed_kt,             # original units, for display
            vrate_fpm=vrate_fpm,
            t=t,
        )

    def update_callsign(self, icao, flight):
        self._st(icao)["flight"] = flight

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
        dop_span = float(d.max() - d.min())
        return FitResult(float(scale), corr, len(s), self.quality(icao),
                         measured, dop_span)

    def joint_fit(self, drift_order=1, min_aircraft=2):
        """Estimate a receiver clock drift g(t) shared across ALL aircraft, plus
        a per-aircraft constant, then report each aircraft's drift-corrected
        Doppler.

        Model (Doppler scale fixed to 1 - the physics is known exactly):

            f_i(t) = c_i + predicted_doppler_i(t) + g(t)

        After subtracting the known predicted Doppler, the leftover time
        variation is the common clock drift, identical for every aircraft, so it
        is identifiable from the ensemble (unlike a per-aircraft linear baseline,
        which wrongly absorbs each aircraft's own near-linear Doppler and inflates
        Scale). The reported Scale is then an honest diagnostic: how well the
        drift-corrected measurement matches predicted (should be ~1).

        Returns {icao: FitResult}. Falls back to independent per-aircraft fits
        when fewer than min_aircraft are available (drift not separable).
        """
        actives = [(ic, self._samples[ic]) for ic in self.icaos()
                   if len(self._samples.get(ic, [])) >= MIN_BURSTS]
        if len(actives) < min_aircraft:
            return {ic: self.fit(ic) for ic, _ in actives}

        all_t = [x.t for _, smp in actives for x in smp]
        t0 = min(all_t)
        tspan = (max(all_t) - t0) or 1.0
        n = len(actives)
        k = drift_order
        m_total = sum(len(smp) for _, smp in actives)

        design = np.zeros((m_total, n + k))
        target = np.zeros(m_total)
        parts = []
        row = 0
        for j, (ic, smp) in enumerate(actives):
            tn = (np.array([x.t for x in smp]) - t0) / tspan
            f = np.array([x.f_offset for x in smp])
            d = np.array([x.doppler_pred for x in smp])
            mlen = len(smp)
            design[row:row + mlen, j] = 1.0                  # per-aircraft const
            for p in range(1, k + 1):
                design[row:row + mlen, n + p - 1] = tn ** p  # shared drift
            target[row:row + mlen] = f - d                   # scale fixed to 1
            parts.append((ic, tn, f, d, mlen))
            row += mlen

        coef, *_ = np.linalg.lstsq(design, target, rcond=None)
        consts = coef[:n]
        drift = coef[n:]

        results = {}
        for j, (ic, tn, f, d, mlen) in enumerate(parts):
            g = sum(drift[p - 1] * tn ** p for p in range(1, k + 1))
            measured = f - consts[j] - g
            mm = measured - measured.mean()
            dd = d - d.mean()
            denom = float(np.dot(dd, dd))
            if denom > 0 and np.dot(mm, mm) > 0:
                scale = float(np.dot(mm, dd) / denom)
                corr = float(np.dot(mm, dd) / np.sqrt(np.dot(mm, mm) * denom))
            else:
                scale, corr = 0.0, 0.0
            results[ic] = FitResult(scale, corr, mlen, self.quality(ic),
                                    measured, float(d.max() - d.min()))
        return results
