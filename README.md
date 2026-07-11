# doppler1090

**Passive aircraft Doppler tracker for RTL-SDR.** It measures the Doppler shift of
ADS-B aircraft on 1090 MHz and plots the *measured* frequency track against the track
*predicted* from each aircraft's own reported position and velocity - measured vs.
physics, side by side, on the same screen.

![doppler1090 dashboard: live aircraft on an FAA sectional chart, with a
measured-vs-predicted Doppler plot for the selected flight](docs/dashboard.jpg)

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
| Capture | `capture.py` | Streams 2.4 Msps IQ chunks from the RTL-SDR at 1090 MHz. |
| Detect | `detect.py` | Magnitude + preamble correlation to find candidate bursts. |
| Decode | `decode.py`, `correct.py` | Vectorized PPM demodulation to a bit matrix; Mode S / ADS-B decode with CRC-syndrome error correction. |
| Estimate | `estimate.py` | Per-burst carrier-frequency offset from the recovered phase. |
| Predict | `geometry.py` | Predicted Doppler from the aircraft's ADS-B position/velocity and the receiver location (ECEF geometry). |
| Track | `track.py` | Accumulates per-aircraft offset tracks over a pass and template-fits them against the predicted curve, with a pass-quality score that surfaces the clean straight-line passes where the measurement is trustworthy. |
| Display | `terminal.py`, `server.py`, `web/` | A live `rich` terminal table, plus a browser dashboard served locally. |

The whole thing is vectorized with NumPy - candidate bursts are demodulated as one
`(N, 112)` matrix rather than a Python call per burst - so it keeps up with a live stream.

## Dashboard

`doppler1090 --web` serves a local browser dashboard (a `rich` terminal table is the
default). It has two linked halves:

- **Map.** Live aircraft over **FAA aeronautical charts** - the same public-domain VFR
  sectionals SkyVector uses, served straight from the FAA's tile service, with a layer
  switcher for VFR Sectional/Terminal and IFR Low/High (or plain OpenStreetMap for
  outside US coverage). Each aircraft is a heading-aligned icon; its ground track is
  drawn one segment at a time and **colored by Doppler sign** - blue approaching, through
  white at closest approach, to red receding. The receiver location is marked in gold.
- **Detail panel.** Click an aircraft to plot its **measured vs. predicted** Doppler over
  the whole pass (blue points = measured per-burst offset, green line = the curve
  predicted from its ADS-B state vector), above a readout of the fit stats - correlation,
  confidence, burst count - and its range, altitude, speed, and track.

## Install & run

Requires Python 3.11+ and an **RTL-SDR Blog v4** dongle (its ~1 ppm TCXO is stable enough
over a single pass for the differential method above) with an antenna for 1090 MHz.

The receiver's location is required - it's the reference point the predicted Doppler is
computed against - so pass your antenna's latitude and longitude:

```sh
pip install -e .
doppler1090 --help
doppler1090 --lat 41.88 --lon -87.63            # live rich terminal table
doppler1090 --lat 41.88 --lon -87.63 --web      # + browser dashboard (default :8080)
```

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
distributed under the same terms.

The name follows the lineage of [`dump1090`](https://github.com/antirez/dump1090)
(Salvatore Sanfilippo, BSD-3-Clause). doppler1090 is an independent Python project: it
reimplements a few of dump1090's ideas (CRC-syndrome error correction, framing constants)
from scratch, and shares design and algorithms with it, not source code. The Mode S
decoding itself is delegated to `pyModeS`.

---

Built by [Aaron Hanson](https://aaronhanson.dev).
