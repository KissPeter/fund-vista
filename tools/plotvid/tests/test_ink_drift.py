import numpy as np

from plotvid import ink


def _analysis(steps):
    stored = np.stack([np.eye(3)] + [np.vstack([m, [0, 0, 1]]) for m in steps])
    return ink.InkAnalysis(fps=10.0, width=1, feats=None, steps=stored, ecc_failures=0)


def _product(steps):
    m = np.eye(3)
    for s in steps:
        m = np.vstack([s, [0, 0, 1]]) @ m
    return m


def test_map_over_short_span_is_exact_after_long_growing_chain():
    # Each step scales up 0.1 %, so the chain from frame 0 grows past 1e8 by the end.
    grow = np.float64([[1.001, 0, 0.5], [0, 1.001, -0.25]])
    steps = [grow] * 6800
    analysis = _analysis(steps)
    for i, j in [(3000, 3010), (5000, 4995), (6790, 6800)]:
        expect = _product(steps[i:j]) if j >= i else np.linalg.inv(_product(steps[j:i]))
        assert np.allclose(analysis._map(i, j), expect[:2], rtol=1e-6, atol=1e-9)


def test_map_identity_when_frames_match():
    analysis = _analysis([np.float64([[1, 0, 0], [0, 1, 0]])] * 5)
    assert np.allclose(analysis._map(2, 2), np.eye(3)[:2])
