import numpy as np
from dataclasses import dataclass
from rich.console import Console
from rich.table import Table
from .geometry import geodetic_to_ecef

_console = Console()

# Predicted-Doppler peak-to-peak (Hz) at which a pass is considered fully
# "observable". Below this the fit is poorly conditioned, so confidence is
# scaled down proportionally.
DOP_SPAN_REF_HZ = 400.0


def confidence(fit):
    """How much to trust this Doppler fit, in [0, 1].

    Combines correlation (does the measured offset track the predicted curve?)
    with observability (did the predicted Doppler actually sweep enough to be
    measurable?). Negative correlation -> 0 (it isn't tracking)."""
    observability = min(1.0, fit.dop_span / DOP_SPAN_REF_HZ)
    return max(0.0, fit.correlation) * observability


@dataclass
class Row:
    icao: str
    flight: str
    alt_ft: float | None
    speed_kt: float | None
    track_deg: float | None
    vrate_fpm: float | None
    range_km: float
    dop_pred: float
    dop_meas: float
    scale: float
    corr: float
    conf: float
    quality: float
    bursts: int


def build_rows(store, rx_llh, min_conf=0.0):
    """Build display rows in stable first-seen order (oldest aircraft first, so
    rows don't reshuffle). Aircraft whose fit confidence is below min_conf are
    omitted."""
    rx = geodetic_to_ecef(*rx_llh)
    rows = []
    for icao in store.icaos():  # dict insertion order == first-seen order
        fit = store.fit(icao)
        if fit is None:
            continue
        conf = confidence(fit)
        if conf < min_conf:
            continue
        s = store.latest(icao)
        rng_km = 0.0
        if all(k in s for k in ("lat", "lon", "alt")):
            ac = geodetic_to_ecef(s["lat"], s["lon"], s["alt"])
            rng_km = float(np.linalg.norm(ac - rx) / 1000.0)
        samples = store._samples[icao]
        rows.append(Row(
            icao=icao,
            flight=s.get("flight", ""),
            alt_ft=s.get("alt"),
            speed_kt=s.get("speed_kt"),
            track_deg=s.get("track"),
            vrate_fpm=s.get("vrate_fpm"),
            range_km=rng_km,
            dop_pred=float(samples[-1].doppler_pred),
            dop_meas=float(fit.measured_doppler[-1]),
            scale=fit.scale,
            corr=fit.correlation,
            conf=conf,
            quality=fit.quality,
            bursts=fit.n,
        ))
    return rows


def build_table(rows):
    """Build the rich Table for the given rows. Pure (no I/O), so it can be
    handed to rich.Live for flicker-free in-place updates."""
    table = Table(title="doppler1090 - measured vs predicted Doppler")
    for col in ("ICAO", "Flight", "Alt ft", "Spd kt", "Trk°", "V/S fpm",
                "Range km", "Dop pred Hz", "Dop meas Hz",
                "Scale", "Corr", "Conf", "Quality", "Bursts"):
        table.add_column(col, justify="right")
    for r in rows:
        table.add_row(
            r.icao, r.flight,
            "-" if r.alt_ft is None else f"{r.alt_ft:.0f}",
            "-" if r.speed_kt is None else f"{r.speed_kt:.0f}",
            "-" if r.track_deg is None else f"{r.track_deg:.0f}",
            "-" if r.vrate_fpm is None else f"{r.vrate_fpm:+.0f}",
            f"{r.range_km:.1f}",
            f"{r.dop_pred:+.0f}", f"{r.dop_meas:+.0f}",
            f"{r.scale:.2f}", f"{r.corr:.2f}", f"{r.conf:.2f}",
            f"{r.quality:.2f}", str(r.bursts),
        )
    return table


def render(rows):
    """One-shot print (used outside a Live context)."""
    _console.print(build_table(rows))
