import numpy as np
from doppler1090.track import TrackStore
from doppler1090.terminal import build_rows


def _seed(store, icao, n, trk):
    for i in range(n):
        store.update_position(icao, float(i), 0.0, 0.01 + 0.0001 * i, 10000.0)
        store.update_velocity(icao, float(i), 480.0, trk, 0.0)
        s = store.latest(icao)
        store._inject(icao, float(i), 1000.0 + 5.0 * i, -200.0 + 10.0 * i,
                      s["lat"], s["lon"], trk)


def test_build_rows_sorted_by_quality_desc():
    store = TrackStore()
    _seed(store, "straight", 40, 270.0)          # high quality
    _seed(store, "turning", 40, 0.0)             # placeholder track
    # make 'turning' actually turn so its quality drops
    for i, x in enumerate(store._samples["turning"]):
        x.track = (i * 9) % 360
    rows = build_rows(store, (0.0, 0.0, 0.0))
    assert [r.icao for r in rows][0] == "straight"
    assert rows[0].quality >= rows[-1].quality
    assert rows[0].bursts == 40
