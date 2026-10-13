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
    buckets_get = buckets.get
    for i in range(n):
        if used[i]:
            continue
        used[i] = True
        cur = list(work[i])
        while True:
            # Points within tol are at most one tol-sized cell apart, so a
            # 3x3 neighborhood is sufficient (the old 5x5 was over-conservative).
            # Split candidates by which stroke end they can touch so the
            # distance filter only runs on plurality-relevant endpoint pairs.
            tail_set: set[int] = set()
            head_set: set[int] = set()
            for cell_of, acc in ((cur[-1], tail_set), (cur[0], head_set)):
                cx, cy = grid_cell(cell_of, tol)
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        hit = buckets_get((cx + dx, cy + dy))
                        if hit:
                            acc.update(hit)
            pick = -1
            for j in sorted(tail_set | head_set):
                if used[j]:
                    continue
                b = work[j]
                joined = False
                if j in tail_set:
                    if dist(cur[-1], b[0]) <= tol:
                        cur = cur + list(b)[1:]
                        joined = True
                    elif dist(cur[-1], b[-1]) <= tol:
                        cur = cur + list(reversed(b))[1:]
                        joined = True
                if not joined and j in head_set:
                    if dist(cur[0], b[-1]) <= tol:
                        cur = list(b) + cur[1:]
                        joined = True
                    elif dist(cur[0], b[0]) <= tol:
                        cur = list(reversed(b)) + cur[1:]
                        joined = True
                if joined:
                    used[j] = True
                    pick = j
                    break
            if pick < 0:
                break
        merged.append(cur)
    log.debug("linemerge: %d -> %d strokes (tol=%.3fmm)", len(lines), len(merged), tol)
    return merged


__all__ = ["linemerge"]
