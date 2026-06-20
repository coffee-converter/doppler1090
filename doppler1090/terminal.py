import numpy as np
from dataclasses import dataclass
from rich.console import Console
from rich.table import Table
from .geometry import geodetic_to_ecef

_console = Console()


@dataclass
class Row:
    icao: str
    flight: str
    range_km: float
    dop_pred: float
    dop_meas: float
    scale: float
    corr: float
    quality: float
    bursts: int


def build_rows(store, rx_llh):
    rx = geodetic_to_ecef(*rx_llh)
    rows = []
    for icao in store.icaos():
        fit = store.fit(icao)
        if fit is None:
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
            range_km=rng_km,
            dop_pred=float(samples[-1].doppler_pred),
            dop_meas=float(fit.measured_doppler[-1]),
            scale=fit.scale,
            corr=fit.correlation,
            quality=fit.quality,
            bursts=fit.n,
        ))
    rows.sort(key=lambda r: r.quality, reverse=True)
    return rows


def build_table(rows):
    """Build the rich Table for the given rows. Pure (no I/O), so it can be
    handed to rich.Live for flicker-free in-place updates."""
    table = Table(title="doppler1090 - measured vs predicted Doppler")
    for col in ("ICAO", "Flight", "Range km", "Dop pred Hz", "Dop meas Hz",
                "Scale", "Corr", "Quality", "Bursts"):
        table.add_column(col, justify="right")
    for r in rows:
        table.add_row(
            r.icao, r.flight, f"{r.range_km:.1f}",
            f"{r.dop_pred:+.0f}", f"{r.dop_meas:+.0f}",
            f"{r.scale:.2f}", f"{r.corr:.2f}",
            f"{r.quality:.2f}", str(r.bursts),
        )
    return table


def render(rows):
    """One-shot print (used outside a Live context)."""
    _console.print(build_table(rows))
