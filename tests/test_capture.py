import numpy as np
from doppler1090.capture import iq_chunks


class FakeSdr:
    def __init__(self):
        self.closed = False
        self._reads = 0

    def read_samples(self, n):
        self._reads += 1
        if self._reads > 2:
            raise KeyboardInterrupt
        return np.zeros(n, dtype=complex)

    def close(self):
        self.closed = True


def test_iq_chunks_yields_then_closes():
    fake = FakeSdr()
    chunks = []
    try:
        for t, iq in iq_chunks(1_090_000_000, 2_400_000, 40, 0,
                               chunk_size=256, sdr_factory=lambda: fake):
            chunks.append((t, iq))
    except KeyboardInterrupt:
        pass
    assert len(chunks) == 2
    assert chunks[0][1].shape == (256,)
    assert fake.closed is True
