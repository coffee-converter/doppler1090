import numpy as np
from dataclasses import dataclass
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text
from .geometry import geodetic_to_ecef, FT_TO_M

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
    make: str | None
    model: str | None
    reg: str | None
    alt_ft: float | None
    speed_kt: float | None
    track_deg: float | None
    vrate_fpm: float | None
    range_km: float
    dop_pred: float
    dop_meas: float
    rssi: float | None
    scale: float
    corr: float
    conf: float
    quality: float
    bursts: int


def build_rows(store, rx_llh, min_conf=0.0, type_store=None):
    """Build display rows in stable first-seen order (oldest aircraft first, so
    rows don't reshuffle). Aircraft whose fit confidence is below min_conf are
    omitted. ``type_store`` (optional) supplies cached make/model/registration -
    a miss just leaves those blank and fills in on a later frame."""
    rx = geodetic_to_ecef(*rx_llh)
    fits = store.joint_fit()  # shared clock-drift removed across all aircraft
    rows = []
    for icao in store.icaos():  # dict insertion order == first-seen order
        fit = fits.get(icao)
        if fit is None:
            continue
        conf = confidence(fit)
        if conf < min_conf:
            continue
        s = store.latest(icao)
        rng_km = 0.0
        if all(k in s for k in ("lat", "lon", "alt")):
            ac = geodetic_to_ecef(s["lat"], s["lon"], s["alt"] * FT_TO_M)
            rng_km = float(np.linalg.norm(ac - rx) / 1000.0)
        samples = store._samples[icao]
        info = type_store.get(icao) if type_store is not None else None
        # mean burst amplitude over the window, as dBFS (matches the web card)
        sigs = [x.signal for x in samples if x.signal > 0]
        rssi = round(float(20 * np.log10(np.mean(sigs))), 1) if sigs else None
        rows.append(Row(
            icao=icao,
            flight=s.get("flight", ""),
            make=(info or {}).get("make"),
            model=(info or {}).get("model"),
            reg=(info or {}).get("reg"),
            alt_ft=s.get("alt"),
            speed_kt=s.get("speed_kt"),
            track_deg=s.get("track"),
            vrate_fpm=s.get("vrate_fpm"),
            range_km=rng_km,
            dop_pred=float(samples[-1].doppler_pred),
            dop_meas=float(fit.measured_doppler[-1]),
            rssi=rssi,
            scale=fit.scale,
            corr=fit.correlation,
            conf=conf,
            quality=fit.quality,
            bursts=fit.n,
        ))
    return rows


# Column width tiers: which columns survive as the terminal narrows. The
# Doppler-fit diagnostics (scale/corr/conf/quality) drop first; standard ADS-B
# fields (heading, vertical speed) and the core Doppler comparison stay longest.
NARROW, COMPACT, FULL = 0, 1, 2


def _tier(width):
    if width is None or width >= 150:
        return FULL
    return COMPACT if width >= 100 else NARROW


def _ident_cell(r):
    # what identifies this aircraft to a human: its transmitted callsign
    # (airliners) or, failing that, its tail number (GA/private)
    return r.flight or r.reg or "-"


def _aircraft_cell(r):
    # just the type now - the tail number lives in the Ident column, so this
    # stays short (a big width win over model + reg)
    return r.model or r.make or "-"


def _dop_cell(r):
    # measured (predicted) - the project's headline comparison, side by side
    return f"{r.dop_meas:+.0f} ({r.dop_pred:+.0f})"


# (header, min_tier, value_fn) - a column shows when the current tier >= min_tier.
# Order reads left to right as identity -> kinematics -> receiver-relative ->
# the Doppler measurement -> fit diagnostics. Tier is independent of position,
# so a compact-only column just drops out in place when the terminal narrows.
_COLUMNS = [
    ("ICAO",        NARROW,  lambda r: r.icao),
    ("Ident",       NARROW,  _ident_cell),
    ("Aircraft",    NARROW,  _aircraft_cell),
    ("Alt ft",      NARROW,  lambda r: "-" if r.alt_ft is None else f"{r.alt_ft:.0f}"),
    ("V/S fpm",     COMPACT, lambda r: "-" if r.vrate_fpm is None else f"{r.vrate_fpm:+.0f}"),
    ("Spd kt",      NARROW,  lambda r: "-" if r.speed_kt is None else f"{r.speed_kt:.0f}"),
    ("Trk°",        NARROW,  lambda r: "-" if r.track_deg is None else f"{r.track_deg:.0f}"),
    ("Range km",    NARROW,  lambda r: f"{r.range_km:.1f}"),
    ("Sig dB",      COMPACT, lambda r: "-" if r.rssi is None else f"{r.rssi:.0f}"),
    ("Dop m(p) Hz", NARROW,  _dop_cell),
    ("Bursts",      COMPACT, lambda r: str(r.bursts)),
    ("Scale",       FULL,    lambda r: f"{r.scale:.2f}"),
    ("Corr",        FULL,    lambda r: f"{r.corr:.2f}"),
    ("Conf",        FULL,    lambda r: f"{r.conf:.2f}"),
    ("Quality",     FULL,    lambda r: f"{r.quality:.2f}"),
]


# Identity columns hold text: left-aligned, and truncated (never wrapped) so a
# long make/model + reg stays on one line - one aircraft, one row.
_TEXT_COLS = {"ICAO", "Ident", "Aircraft"}


# Approx width the non-Aircraft columns of each tier need (content + padding).
# Aircraft gets whatever's left, so the numeric columns always keep their digits
# rather than rich shrinking everything proportionally.
_NONAC_BUDGET = {NARROW: 76, COMPACT: 112, FULL: 156}


def build_table(rows, width=None):
    """Build the rich Table, dropping lower-priority columns to fit ``width``
    (None = full). The identity 'Aircraft' column absorbs the width squeeze
    (ellipsized) so numbers never wrap or truncate. Pure (no I/O), so it can be
    handed to rich.Live for flicker-free in-place updates."""
    tier = _tier(width)
    cols = [c for c in _COLUMNS if c[1] <= tier]
    # cap Aircraft to the leftover width so the numeric columns stay whole
    ac_cap = None if width is None else max(8, width - _NONAC_BUDGET[tier])
    table = Table()
    for header, _, _ in cols:
        if header == "Aircraft":
            table.add_column(header, justify="left", no_wrap=True,
                             overflow="ellipsis", max_width=ac_cap)
        elif header in _TEXT_COLS:            # ICAO, Flight
            table.add_column(header, justify="left", no_wrap=True,
                             overflow="ellipsis")
        else:                                 # numeric: never wrap a number
            table.add_column(header, justify="right", no_wrap=True)
    for r in rows:
        table.add_row(*[fn(r) for _, _, fn in cols])
    return table


_SDR_STYLE = {"receiving": "bold green", "idle": "bold yellow",
              "down": "bold red", "replay": "bold cyan"}


def _fmt_dur(s):
    s = int(s)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def build_header(status=None, clock=None):
    """A status line (receiver, uptime, aircraft, decode rate, SDR light) plus,
    when available, the ppm self-calibration readout - the same figures the web
    dashboard shows, so the terminal is where you'd read and act on --ppm."""
    st = status or {}
    title = Text("doppler1090", style="bold")
    title.append(" — live ADS-B & measured-vs-predicted Doppler", style="dim")
    line = Text()
    rx = st.get("rx")
    if rx:
        line.append(f"rx {rx[0]:.4f},{rx[1]:.4f}  ", style="cyan")
    line.append(f"up {_fmt_dur(st.get('uptime_s', 0))}  ")
    line.append(f"{st.get('n_aircraft', 0)} ac  ")
    line.append(f"{st.get('burst_rate', 0):.0f} brst/min  ")
    state = st.get("sdr_state", "down")
    line.append("● ", style=_SDR_STYLE.get(state, "dim"))
    line.append(state)
    parts = [title, line]
    if clock:
        off = clock["offset_ppm"]
        cur = clock.get("ppm") or 0
        c = Text("ppm  ", style="bold")
        c.append(f"{off:+.1f}", style="magenta")
        c.append(f"  suggest --ppm {round(off)}  "
                 f"(now --ppm {cur}, residual {off - cur:+.1f})")
        drift = clock.get("drift_ppm_min")
        if drift is not None:
            c.append(f"  drift {drift:+.2f} ppm/min")
        if not clock.get("fresh_n"):        # dim while not fitting right now
            c.stylize("dim")
        parts.append(c)
    return Group(*parts)


def build_display(rows, status=None, clock=None, width=None):
    """Full terminal frame: the status/ppm header above the aircraft table,
    with the table's columns fitted to ``width``."""
    return Group(build_header(status, clock), build_table(rows, width))


def render(rows, status=None, clock=None):
    """One-shot print (used outside a Live context)."""
    _console.print(build_display(rows, status, clock, _console.size.width))
