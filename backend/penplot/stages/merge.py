"""Linemerge stage (REF-002 Phase 4).

Pure move from :mod:`backend.penplot.optimize` — identical behavior.
The nested ``cell()`` closure is replaced by the shared
:func:`backend.penplot.stages._grid.grid_cell` (same math, tol-sized).
"""

from __future__ import annotations

import logging

from backend.penplot.methods import Polyline
from backend.penplot.stages._grid import dist, grid_cell

log = logging.getLogger(__name__)


def linemerge(lines: list[Polyline], tol: float) -> list[Polyline]:
    """Greedily join endpoint-touching polylines (any orientation).

    Semantics identical to the original: seed each merged stroke from the
    first unused line in input order, then repeatedly absorb the *first
    unused* line that touches the growing stroke at either end (four
    orientation checks in a fixed priority). Neighbor lookup now uses a
    spatial hash over endpoints instead of an O(n) scan per step — the old
    version was O(n^2)/O(n^3) on dense diagrams (a 5000-stroke airport SVG
    spent ~10 s merging; the hash drops that to milliseconds).
    """
    if tol <= 0 or len(lines) < 2:
        return [list(pl) for pl in lines]
    work = [list(pl) for pl in lines if len(pl) >= 2]
    if len(work) < 2:
        return work
    n = len(work)

    buckets: dict[tuple[int, int], set[int]] = {}
    for i, pl in enumerate(work):
        for end in (pl[0], pl[-1]):
            buckets.setdefault(grid_cell(end, tol), set()).add(i)
    used = [False] * n
    merged: list[Polyline] = []
    for i in range(n):
        if used[i]:
            continue
        used[i] = True
        cur = list(work[i])
        while True:
            candidates: set[int] = set()
            for end in (cur[0], cur[-1]):
                cx, cy = grid_cell(end, tol)
                for dx in range(-2, 3):
                    for dy in range(-2, 3):
                        hit = buckets.get((cx + dx, cy + dy))
                        if hit:
                            candidates |= hit
            pick = -1
            for j in sorted(candidates):
                if used[j]:
                    continue
                b = work[j]
                if dist(cur[-1], b[0]) <= tol:
                    cur = cur + list(b)[1:]
                elif dist(cur[-1], b[-1]) <= tol:
                    cur = cur + list(reversed(b))[1:]
                elif dist(cur[0], b[-1]) <= tol:
                    cur = list(b) + cur[1:]
                elif dist(cur[0], b[0]) <= tol:
                    cur = list(reversed(b)) + cur[1:]
                else:
                    continue
                used[j] = True
                pick = j
                break
            if pick < 0:
                break
        merged.append(cur)
    log.debug("linemerge: %d -> %d strokes (tol=%.3fmm)", len(lines), len(merged), tol)
    return merged


__all__ = ["linemerge"]
