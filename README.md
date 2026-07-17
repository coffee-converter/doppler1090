# doppler1090

**Passive aircraft Doppler tracker for RTL-SDR.** It measures the Doppler shift of
ADS-B aircraft on 1090 MHz and plots the *measured* frequency track against the track
*predicted* from each aircraft's own reported position and velocity - measured vs.
physics, side by side, on the same screen.

![doppler1090 dashboard: live aircraft on an FAA sectional chart, a
measured-vs-predicted Doppler plot for the selected flight, and a session
time-travel scrubber](docs/dashboard.jpg)

## The idea

ADS-B is transmitted on a nominal 1090 MHz carrier, so a real Doppler shift is
physically present in the signal. Two things normally hide it:

- **Envelope decoders throw frequency away.** Standard ADS-B decoders take sample
  magnitude to find the on/off pulses; the carrier phase - where Doppler lives - is
  discarded before decoding.
- **Clock error dwarfs the signal.** Doppler at 1090 MHz for a fast airliner (~250 m/s
  radial) is only ~900 Hz, but a stock RTL-SDR oscillator is off by several kHz and
  drifts with temperature.

**The trick: never trust an absolute frequency - track the change.** The unknown
constant (transmitter center offset + receiver clock offset) is roughly fixed over the
few minutes of a pass, while Doppler is time-varying with a characteristic signature: an
aircraft sweeps from about +900 Hz (approaching) through 0 (closest approach) to about
-900 Hz (receding) - a swing up to ~1.8 kHz. Measuring the *change* cancels the unknowns.

And because every ADS-B message carries position and velocity, the predicted Doppler
curve is available the instant a message decodes - **ground truth for free**, no separate
validation step. 1090 MHz is uniquely suited to this: it's the only aviation signal that
reports the transmitter's own state vector, which is exactly what supplies the reference
for an otherwise uncooperative transmitter.

## How it works

A straight pipeline from raw IQ to a measured-vs-predicted comparison:

| Stage | Module | What it does |
|-------|--------|--------------|
| Capture | `capture.py` | Streams 2 Msps IQ chunks from the RTL-SDR at 1090 MHz. |
| Detect | `detect.py` | Magnitude + preamble correlation to find candidate bursts. |
| Decode | `decode.py`, `correct.py` | Vectorized PPM demodulation to a bit matrix; Mode S / ADS-B decode with CRC-syndrome error correction. |
| Estimate | `estimate.py` | Per-burst carrier-frequency offset from the recovered phase. |
| Predict | `geometry.py` | Predicted Doppler from the aircraft's ADS-B position/velocity and the receiver location (ECEF geometry). |
| Track | `track.py` | Accumulates per-aircraft offset tracks over a pass and template-fits them against the predicted curve, with a joint clock model shared across all aircraft (the basis of the ppm self-calibration) and a pass-quality score that surfaces the clean straight-line passes where the measurement is trustworthy. |
| Record | `history.py` | Appends every decoded state message and Doppler burst to a per-session SQLite log, and reconstructs the full state as of any past instant for the dashboard's time slider. |
| Accrue | `records.py`, `coverage.py`, `clockcal.py` | Persistent all-time stores: extreme-value records, per-bearing reception coverage, and the accumulated oscillator-offset calibration. |
| Display | `terminal.py`, `server.py`, `web/` | A live `rich` terminal table, plus a browser dashboard with a time-travel scrubber, served locally. |

The whole thing is vectorized with NumPy - candidate bursts are demodulated as one
`(N, 112)` matrix rather than a Python call per burst - so it keeps up with a live stream.

## Dashboard

`doppler1090 --web` serves a local browser dashboard (a `rich` terminal table is the
default). The map is a fullscreen "scope"; a left panel and a bottom scrubber float over it.

- **Map.** Live aircraft over **FAA aeronautical charts** - the same public-domain VFR
  sectionals SkyVector uses, served straight from the FAA's tile service, with a layer
  switcher for the VFR Sectional, an auto **IFR** enroute chart (high-altitude when zoomed
  out, low-altitude when zoomed in), or plain OpenStreetMap for outside US coverage, plus an
  optional **Coverage** overlay that draws your per-bearing farthest-heard range as a
  reception footprint around the receiver. Each aircraft is a **type-accurate silhouette**
  aligned to its heading and stacked by altitude (highest on top); click one - or click a
  congested cluster repeatedly - to cycle through the planes under the pointer. Its ground
  track is painted as a smooth **Doppler gradient** - blue approaching, through white at
  closest approach, to red receding - interpolated from the predicted curve so the colour
  reads correctly even across a coarse or overhead segment. The receiver is marked in gold
  with concentric nautical-mile range rings, and the selected aircraft gets a line-of-sight
  to the receiver. When an aircraft stops transmitting it lingers as a fading, desaturated
  "ghost" for a few minutes before dropping off, so recent passes stay in view.
- **Left panel.** A status header - an **SDR status light** (green receiving / amber no-ADS-B
  / red offline), receiver, uptime, aircraft count, decode rate, and the estimated
  **oscillator offset** (see Calibration below) - above a rotating **all-time records** ticker
  (fastest/slowest, highest/lowest, biggest climb/descent, farthest/nearest, strongest/
  weakest signal, widest Doppler swing, each with the flight that set it) and a list of
  aircraft cards - callsign, type, fit confidence, and a Doppler sparkline - in stable
  first-seen order. Silent aircraft collapse to compact pills; click one to expand it. The
  expanded card names the aircraft's **make, model, and registration** and shows a **photo**
  when one is available (all resolved from its Mode S address - see below), then plots its
  **measured vs. predicted** Doppler over the whole pass (blue points = measured per-burst
  offset, green line = the curve predicted from its ADS-B state vector, with 0 Hz centered),
  above its fit stats - correlation, confidence, burst count, signal strength - and its
  range, altitude, speed, and track.
- **Time travel.** Every session is recorded, so the bottom scrubber can replay it: drag
  the playhead or hit play to animate past aircraft tracks over an activity-density strip,
  then jump back to **LIVE**. Capture keeps running and recording the whole time you scrub.
- **Self-calibration.** Since every aircraft transmits its own position, the *predicted*
  Doppler is known, and the leftover offset is the receiver's own clock error. doppler1090
  fits a clock model shared across all aircraft in view and reports the estimated **receiver
  oscillator offset in ppm** - a robust median over a rolling 24 h window, with a suggested
  `--ppm` - turning passing aircraft into a frequency reference that calibrates your SDR. It
  accumulates across sessions and is invariant to the `--ppm` in force (offsets from every
  setting combine).

## Install & run

Requires Python 3.11+ and an RTL-SDR (RTL2832U) dongle with an antenna for 1090 MHz. Any
RTL-SDR runs it, but an **RTL-SDR Blog v4** is recommended: its ~1 ppm TCXO stays stable
over a single pass, which the differential method above relies on. Generic or older
non-TCXO dongles still decode fine, but oscillator drift within a pass adds noise to the
Doppler measurement and the self-calibration. (Non-RTL hardware - Airspy, SDRplay, HackRF -
isn't supported without code changes; capture is `pyrtlsdr`-specific.)

The receiver's location is required - it's the reference point the predicted Doppler is
computed against - so pass your antenna's latitude and longitude (and, for accurate range,
its elevation in feet via `--alt`):

```sh
pip install -e .
doppler1090 --help
doppler1090 --lat 41.88 --lon -87.63                 # live rich terminal table
doppler1090 --lat 41.88 --lon -87.63 --alt 600 --web # + browser dashboard (default :8080)
doppler1090 --lat 41.88 --lon -87.63 --web --faa-registry   # + offline US make/model lookup
```

Make, model, and registration are resolved from each aircraft's Mode S address via the free
[adsbdb](https://www.adsbdb.com/) API, and a photo from
[planespotters](https://www.planespotters.net/) (falling back to airport-data.com, then to a
[Wikimedia Commons](https://commons.wikimedia.org/) photo of the make/model when a specific
tail has none) - all cached to the data dir and shared across sessions. `--faa-registry` adds a one-time ~73 MB
download of the FAA aircraft registry for offline, US-complete coverage (it fills in the
private/GA tails the API misses). Lookups run on a background thread, so a miss just fills
in on a later frame and never blocks capture.

Each run is recorded to `./doppler1090-data/` (one SQLite file per session, tagged with the
`--ppm` in use) so the dashboard's time slider can replay it; pass `--no-record` to disable
or `--data-dir` to put the logs elsewhere. The dashboard also estimates your receiver's
oscillator offset in ppm from the aircraft themselves - once it settles, pass the suggested
value as `--ppm` and it should trend toward zero.

## Tests

```sh
pip install pytest && pytest      # 65 tests across the pipeline
```

Every stage has a unit test (`tests/test_*.py`) - capture, detect, decode, estimate,
correct, geometry, track, terminal, server, CLI, and web assets.

## Built with

Python · NumPy · SciPy · [pyModeS](https://github.com/junzis/pyModeS) (Mode S decoding) ·
[pyrtlsdr](https://github.com/pyrtlsdr/pyrtlsdr) (RTL-SDR capture) · `rich` (terminal UI) ·
[Leaflet](https://leafletjs.com) with [FAA aeronautical charts](https://www.faa.gov/air_traffic/flight_info/aeronav/)
and [OpenStreetMap](https://www.openstreetmap.org/copyright) tiles (web map)

## License & acknowledgments

Licensed under the **GNU General Public License v3.0** - see [LICENSE](LICENSE).
`doppler1090` links against `pyModeS` and `pyrtlsdr`, both GPLv3, so the combined work is
distributed under the same terms. The aircraft silhouette icons are the marker set from
[tar1090](https://github.com/wiedehopf/tar1090) (GPLv3), vendored under `web/vendor/`.

The name follows the lineage of [`dump1090`](https://github.com/antirez/dump1090)
(Salvatore Sanfilippo, BSD-3-Clause). doppler1090 is an independent Python project: it
reimplements a few of dump1090's ideas (CRC-syndrome error correction, framing constants)
from scratch, and shares design and algorithms with it, not source code. The Mode S
decoding itself is delegated to `pyModeS`.

---

Built by [Aaron Hanson](https://aaronhanson.dev).
