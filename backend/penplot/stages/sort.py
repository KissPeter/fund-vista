"""Ordering stages: sort + reloop (REF-002 Phase 4).

Pure move from :mod:`backend.penplot.optimize` — identical behavior.
The nested ``cell()`` closure is replaced by the shared
:func:`backend.penplot.stages._grid.grid_cell` (same math); the
``cell`` callable parameter of :func:`_linesort_nearest` becomes an
explicit ``cell_size`` for the same reason.
"""

from __future__ import annotations

import logging
import math

import numpy as np

from backend.penplot.methods import Polyline
from backend.penplot.stages._grid import dist, grid_cell

log = logging.getLogger(__name__)


def _cell_min_dist(pen: tuple[float, float], cell: tuple[int, int], cell_size: float) -> float:
    """Distance from ``pen`` to the nearest corner of the cell rectangle.

    No point inside the cell can be closer to the pen than this, which is
    what lets the ring search stop early without missing neighbors.
    """
    x0, x1 = cell[0] * cell_size, (cell[0] + 1) * cell_size
    y0, y1 = cell[1] * cell_size, (cell[1] + 1) * cell_size
    dx = 0.0 if x0 <= pen[0] <= x1 else min(abs(pen[0] - x0), abs(pen[0] - x1))
    dy = 0.0 if y0 <= pen[1] <= y1 else min(abs(pen[1] - y0), abs(pen[1] - y1))
    return math.hypot(dx, dy)


def _linesort_nearest(
    pen: tuple[float, float],
    work: list[Polyline],
    available: set[int],
    buckets: dict[tuple[int, int], set[int]],
    cell_size: float,
) -> tuple[int, bool]:
    """Greedy nearest-neighbour selection with the legacy tie-break rules.

    Returns ``(index, reverse)`` for the first-available stroke at the
    minimum end-distance (earliest index wins distance ties; forward
    preferred within the winner). A bounded ring expansion over the spatial
    hash finds the winner in sparse-searching time; when a drawing is too
    sparse for the grid to pay off, the exact legacy linear scan is used
    instead (identical selection, just slower on that rare case).
    """
    best_d = math.inf
    best_j = -1
    best_rev = False
    cx, cy = grid_cell(pen, cell_size)
    scanned = 0
    r = 0
    budget = max(128, 16 * int(math.sqrt(max(len(available), 1))))
    complete = False
    while True:
        if r == 0:
            coords = ((cx, cy),)
        else:
            coords = (
                [(cx + dx, cy - r) for dx in range(-r, r + 1)]
                + [(cx + dx, cy + r) for dx in range(-r, r + 1)]
                + [(cx - r, cy + dy) for dy in range(-r + 1, r)]
                + [(cx + r, cy + dy) for dy in range(-r + 1, r)]
            )
        ring_min = math.inf
        for X, Y in coords:
            scanned += 1
            ring_min = min(ring_min, _cell_min_dist(pen, (X, Y), cell_size))
            hits = buckets.get((X, Y))
            if not hits:
                continue
            for j in hits:
                if j not in available:
                    continue
                cand = work[j]
                d_fwd = dist(pen, cand[0])
                d_rev = dist(pen, cand[-1])
                d, rev = (d_rev, True) if d_rev < d_fwd else (d_fwd, False)
                if d < best_d or (d == best_d and j < best_j):
                    best_d, best_j, best_rev = d, j, rev
        if best_j >= 0 and ring_min > best_d:
            complete = True
            break
        if scanned > budget:
            break
        r += 1
    if not complete:
        # Grid search ran out of budget (sparse defence) — reproduce the
        # exact legacy selection, identical tie-breaks. Correctness never
        # depends on the hash; it is only an accelerator for the common,
        # dense case.
        best_d = math.inf
        best_j = -1
        best_rev = False
        for j in sorted(available):
            cand = work[j]
            d_fwd = dist(pen, cand[0])
            d_rev = dist(pen, cand[-1])
            if d_fwd <= d_rev and d_fwd < best_d:
                best_j, best_rev, best_d = j, False, d_fwd
            elif d_rev < best_d:
                best_j, best_rev, best_d = j, True, d_rev
    return best_j, best_rev


def linesort(lines: list[Polyline]) -> list[Polyline]:
    """Reorder (and maybe reverse) strokes to minimise pen-up travel.

    Same greedy nearest-neighbour rule as before — earliest remaining stroke
    at the minimum end-distance wins, forward first on a tie — but the scan
    hits a spatial hash with ring expansion instead of every remaining
    stroke (old worst case ~O(n^2), ~1.7 s on 5000 strokes).
    """
    if len(lines) < 2:
        return [list(pl) for pl in lines]
    work = [list(pl) for pl in lines]
    n = len(work)
    # Grid scale from the drawing's span so the nearest stroke usually sits
    # in the pen's own cell (ring radius 0-1) without exploding on huge,
    # on-page spans.
    xs = [p[0] for pl in work for p in pl]
    ys = [p[1] for pl in work for p in pl]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
    cell_size = min(8.0, max(0.5, span / 200.0))

    buckets: dict[tuple[int, int], set[int]] = {}
    for i, pl in enumerate(work):
        for end in (pl[0], pl[-1]):
            buckets.setdefault(grid_cell(end, cell_size), set()).add(i)
    available = set(range(n))
    ordered: list[Polyline] = [work[0]]
    available.remove(0)
    pen = ordered[0][-1]
    while available:
        j, rev = _linesort_nearest(pen, work, available, buckets, cell_size)
        nxt = list(reversed(work[j])) if rev else list(work[j])
        available.discard(j)
        ordered.append(nxt)
        pen = nxt[-1]
    return ordered


def reloop(lines: list[Polyline], tol: float) -> list[Polyline]:
    """Rotate closed loops so the seam sits nearest the previous pen position.

    P4: the seam search uses ``np.argmin`` (first-index tie-break, same as the
    previous ``min(range(...))``), so the chosen seam and rotation are
    unchanged. ``tol < 0`` is the fast-path sentinel: loops are left alone.
    """
    if tol < 0:
        return lines
    out: list[Polyline] = []
    prev_end: tuple[float, float] | None = None
    for pl in lines:
        if len(pl) >= 4 and dist(pl[0], pl[-1]) <= max(tol, 1e-9):
            body = pl[:-1]  # drop duplicated closure point for rotation
            anchor = prev_end if prev_end is not None else body[0]
            arr = np.asarray(body, dtype=float)
            d = np.hypot(arr[:, 0] - anchor[0], arr[:, 1] - anchor[1])
            k = int(np.argmin(d))
            rotated = body[k:] + body[:k] + [body[k]]
            out.append(rotated)
            prev_end = rotated[-1]
        else:
            out.append(pl)
            prev_end = pl[-1]
    return out


__all__ = ["linesort", "reloop"]
