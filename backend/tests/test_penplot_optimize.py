"""Equivalence + perf guards for the P3/P4 optimize rewrites.

The reference implementations below are frozen copies of the pre-P3/P4
``backend/penplot/optimize.py`` (git HEAD). The new grid/numpy versions must
produce byte-identical output on random + adversarial inputs — the whole
optimization chain is byte-asserted by the interpolation of the pipeline
("same input always yields byte-identical SVG"). Performance guards keep a
regression to the O(n²) scans from silently sneaking back in.
"""

from __future__ import annotations

import math
import random
import time

from backend.penplot.optimize import (
    linemerge,
    linesort,
    polyline_length,
    reloop,
)

Polyline = list[tuple[float, float]]


# -- frozen references (pre-rewrite behaviour) ----------------------------

def _ref_dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _ref_linemerge(lines: list[Polyline], tol: float) -> list[Polyline]:
    if tol <= 0 or len(lines) < 2:
        return [list(pl) for pl in lines]
    work = [list(pl) for pl in lines if len(pl) >= 2]
    changed = True
    while changed:
        changed = False
        used = [False] * len(work)
        merged: list[Polyline] = []
        for i, a in enumerate(work):
            if used[i]:
                continue
            used[i] = True
            cur = list(a)
            extended = True
            while extended:
                extended = False
                for j, b in enumerate(work):
                    if used[j]:
                        continue
                    joined: Polyline | None = None
                    if _ref_dist(cur[-1], b[0]) <= tol:
                        joined = cur + list(b)[1:]
                    elif _ref_dist(cur[-1], b[-1]) <= tol:
                        joined = cur + list(reversed(b))[1:]
                    elif _ref_dist(cur[0], b[-1]) <= tol:
                        joined = list(b) + cur[1:]
                    elif _ref_dist(cur[0], b[0]) <= tol:
                        joined = list(reversed(b)) + cur[1:]
                    if joined is not None:
                        cur = joined
                        used[j] = True
                        extended = True
                        changed = True
                        break
            merged.append(cur)
        work = merged
    return work


def _ref_linesort(lines: list[Polyline]) -> list[Polyline]:
    if len(lines) < 2:
        return [list(pl) for pl in lines]
    remaining = [list(pl) for pl in lines]
    ordered = [remaining.pop(0)]
    pen = ordered[0][-1]
    while remaining:
        best_j, best_rev, best_d = -1, False, math.inf
        for j, cand in enumerate(remaining):
            d_fwd = _ref_dist(pen, cand[0])
            d_rev = _ref_dist(pen, cand[-1])
            if d_fwd <= d_rev and d_fwd < best_d:
                best_j, best_rev, best_d = j, False, d_fwd
            elif d_rev < best_d:
                best_j, best_rev, best_d = j, True, d_rev
        nxt = remaining.pop(best_j)
        if best_rev:
            nxt = list(reversed(nxt))
        ordered.append(nxt)
        pen = nxt[-1]
    return ordered


def _ref_reloop(lines: list[Polyline], tol: float) -> list[Polyline]:
    if tol < 0:
        return lines
    out: list[Polyline] = []
    prev_end: tuple[float, float] | None = None
    for pl in lines:
        if len(pl) >= 4 and _ref_dist(pl[0], pl[-1]) <= max(tol, 1e-9):
            body = pl[:-1]
            anchor = prev_end if prev_end is not None else body[0]
            k = min(range(len(body)), key=lambda i: _ref_dist(body[i], anchor))
            rotated = body[k:] + body[:k] + [body[k]]
            out.append(rotated)
            prev_end = rotated[-1]
        else:
            out.append(pl)
            prev_end = pl[-1]
    return out


def _ref_polyline_length(pl: Polyline) -> float:
    return sum(_ref_dist(pl[i], pl[i + 1]) for i in range(len(pl) - 1))


# -- fixtures -------------------------------------------------------------

def _seg(rng: random.Random, grid: float = 8.0, touchy: bool = False) -> Polyline:
    # Quantized endpoints so near-collisions happen (grid == the merge tol in
    # several tests); ``touchy`` generates shared-endpoint chains deliberately.
    x0, y0 = round(rng.uniform(0, 50), 4), round(rng.uniform(0, 50), 4)
    if touchy:
        half = max(1, int(abs(grid)))
        dx, dy = rng.randint(-half, half), rng.randint(-half, half)
    else:
        dx, dy = rng.uniform(-grid, grid), rng.uniform(-grid, grid)
    return [(x0, y0), (round(x0 + dx, 4), round(y0 + dy, 4))]


def _closed_ring(rng: random.Random) -> Polyline:
    cx, cy = rng.uniform(0, 100), rng.uniform(0, 100)
    radius = rng.uniform(1, 8)
    n = rng.randint(4, 12)
    ring = [(cx + radius * math.cos(2 * math.pi * k / n),
             cy + radius * math.sin(2 * math.pi * k / n)) for k in range(n)]
    return ring + [ring[0]]


def _random_polylines(rng: random.Random, count: int, touchy: bool = False) -> list[Polyline]:
    return [_seg(rng, touchy=touchy) for _ in range(count)]


# -- equivalence ----------------------------------------------------------

def test_linemerge_equiv_random_touches_with_ties():
    rng = random.Random(1234)
    # Chains sharing endpoints: merging must pick the same first-index line
    # and the same orientation precedence as the reference.
    base = [
        [(0.0, 0.0), (0.0, 5.0)],
        [(0.0, 5.0), (0.0, 10.0)],
        [(0.0, 10.0), (0.0, 3.0)],
        [(5.0, 0.0), (0.0, 0.0)],
        [(0.0, 0.5), (3.0, 4.0)],
    ]
    lines = base + _random_polylines(rng, 300, touchy=True)
    for tol in (0.0, 0.2, 1.0, 1.5):
        assert linemerge(lines, tol) == _ref_linemerge(lines, tol), tol


def test_linemerge_equiv_graduated_tolerances():
    rng = random.Random(99)
    lines = _random_polylines(rng, 500, touchy=True)
    for tol in (0.05, 0.3, 0.7, 2.0):
        assert linemerge(lines, tol) == _ref_linemerge(lines, tol), tol


def test_linemerge_small_inputs():
    assert linemerge([], 1.0) == _ref_linemerge([], 1.0)
    assert linemerge([[(0, 0), (1, 1)]], 1.0) == _ref_linemerge([[(0, 0), (1, 1)]], 1.0)
    assert linemerge([[(0, 0), (1, 1)], [(1, 1), (2, 2)]], 0.0) == \
        _ref_linemerge([[(0, 0), (1, 1)], [(1, 1), (2, 2)]], 0.0)


def test_linesort_equiv_random_and_ties():
    rng = random.Random(7)
    lines = _random_polylines(rng, 400)
    assert linesort(lines) == _ref_linesort(lines)
    # Crafted equal-distance tie: pen equidistant from two candidates —
    # forward must win on d_fwd <= d_rev, and the lower index on equal f.
    ties = [
        [(0.0, 0.0), (0.0, 1.0)],
        [(1.0, 0.0), (2.0, 0.0)],   # pen->start = 1.0, pen->end = sqrt(5)
        [(-1.0, 0.0), (-2.0, 0.0)],  # pen->start = 1.0
        [(0.0, -1.0), (0.0, -2.0)],  # pen->start = 1.0, reversed 2.0
    ]
    # Start the sort pen at (0, 0): stroke 0 starts there, closest ties hit.
    seeded = [[(0.0, 0.0), (0.0, 0.5)]] + ties
    assert linesort(seeded) == _ref_linesort(seeded)


def test_reloop_equiv_random_rings_and_lines():
    rng = random.Random(5)
    lines = []
    for _ in range(120):
        ring = _closed_ring(rng)
        lines.append(ring)
        if rng.random() < 0.5:
            lines.append(_seg(rng))
    for tol in (0.0, 0.05, 0.5, 3.0):
        assert reloop(lines, tol) == _ref_reloop(lines, tol), tol


def test_reloop_symmetric_seams_stable():
    # Symmetric square rings: seams around equal-distance vertices must not
    # flip between the vec and scalar seam searches.
    ring = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
    lines = [ring] * 6
    assert reloop(lines, 0.05) == _ref_reloop(lines, 0.05)


def test_polyline_length_matches_reference():
    rng = random.Random(21)
    for _ in range(400):
        n = rng.randint(2, 40)
        pl = [(rng.uniform(0, 100), rng.uniform(0, 100)) for _ in range(n)]
        new, ref = polyline_length(pl), _ref_polyline_length(pl)
        assert math.isclose(new, ref, rel_tol=1e-12, abs_tol=1e-9)
    assert polyline_length([(0.0, 0.0)]) == 0.0


# -- performance guards ---------------------------------------------------
# Generous bounds: they exist to catch a reversion to quadratic scans, not to
# benchmark. The P3/P4 issues target linemerge < 1 s (medium) / < 10 s (dense),
# linesort < 5 s, reloop < 1 s.

def test_linemerge_medium_is_fast():
    rng = random.Random(3)
    lines = _random_polylines(rng, 4000, touchy=True)
    t0 = time.perf_counter()
    linemerge(lines, 0.5)
    assert time.perf_counter() - t0 < 5.0


def test_linemerge_dense_is_sublinear():
    # 20k segments at a single-cell cluster: quadratic would take minutes.
    rng = random.Random(4)
    lines = _random_polylines(rng, 20000, touchy=True)
    t0 = time.perf_counter()
    linemerge(lines, 0.5)
    assert time.perf_counter() - t0 < 10.0


def test_linesort_is_fast():
    rng = random.Random(5)
    lines = _random_polylines(rng, 2000)
    t0 = time.perf_counter()
    linesort(lines)
    assert time.perf_counter() - t0 < 5.0


def test_reloop_is_fast():
    rng = random.Random(6)
    rings = [_closed_ring(rng) for _ in range(3000)]
    t0 = time.perf_counter()
    reloop(rings, 0.05)
    assert time.perf_counter() - t0 < 2.0


def test_polyline_length_is_fast():
    rng = random.Random(8)
    lines = [
        [(rng.uniform(0, 100), rng.uniform(0, 100)) for _ in range(20)]
        for _ in range(5000)
    ]
    t0 = time.perf_counter()
    for pl in lines:
        polyline_length(pl)
    assert time.perf_counter() - t0 < 2.0