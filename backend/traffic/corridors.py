"""Corridor collapse — merge near-identical movement strokes (the runway funnel).

Around the runways every arrival/departure trace runs on practically the same
geometry (final approach to the threshold, the climb-out leg). Rendered as one
polyline per movement, hundreds of strokes land on the same few millimetres of
paper and each stroke is a full pen pass — the pen re-inks one line dozens of
times and cuts through the sheet. ``linemerge`` in the plot pipeline cannot
help: it joins strokes that touch at their *endpoints*, it never merges
strokes that run **on top of each other**.

The fix belongs here, in the traffic renderer (one render drives both the
preview and the plot — preview = plot, no drift). Per movement layer, a stroke
becomes a *corridor representative* (the seed); later strokes of the same
layer within the corridor tolerance in **both** directions (approximate
Hausdorff) join it instead of being drawn. After :data:`CORRIDOR_SPLIT` joins
the seed retires and the next matching stroke opens a new representative, so a
200-flight funnel still suggests density with one line per ~N flights instead
of 200 overlapping passes.

Pure geometry, layer-local: collapses only where strokes really retrace, and
traces are only near-identical in the funnels and holding stacks — everywhere
else nothing changes (plan: corridor-collapse, owner decision 2026-10-27).

Semantics is ported from ``tools/airport_traffic/corridors.py`` (same
constants, same greedy pass) — keep the two in sync. This copy vectorises the
directed-distance check with numpy (a backend dependency); the POC keeps a
pure-Python equivalent, which is fine: a dense funnel solved for 4000 strokes
sequentially would cost tens of seconds, which is why the did the
vectorisation *first* thing and the sample sweep is the only hot loop.
"""

from __future__ import annotations

import math

import numpy as np

#: Corridor tolerance as a fraction of the canvas width. At the default
#: 1000-wide canvas this is 2 user units; the sheet plots at 0.3 mm/unit, so
#: 2 units ≈ 0.6 mm — about two pen-widths apart, where separate passes only
#: over-ink the same line.
CORRIDOR_TOL_FRAC = 0.002

#: After this many joins a corridor seed retires and the next joining stroke
#: becomes a new representative — the corridor keeps one line per this many
#: flights instead of collapsing to a single stroke.
CORRIDOR_SPLIT = 8

#: Resample step for the distance estimate, as a fraction of the tolerance.
#: At 0.25 the sampled Hausdorff stays within half a step (a quarter of the
#: tolerance) of the true one against the chord-broken polyline.
_SAMPLE_STEP_FRAC = 0.25


def _samples(pl, step: float) -> np.ndarray:
    """Resample a polyline to points at most ``step`` apart -> (N, 2) float array."""
    out: list[tuple[float, float]] = [pl[0]]
    for (x0, y0), (x1, y1) in zip(pl, pl[1:]):
        length = math.hypot(x1 - x0, y1 - y0)
        if length == 0.0:
            continue
        k = 1
        while k * step < length:
            f = k * step / length
            out.append((x0 + f * (x1 - x0), y0 + f * (y1 - y0)))
            k += 1
        out.append((x1, y1))
    return np.asarray(out, dtype=float)


def _directed(samples: np.ndarray, segs, limit: float) -> bool:
    """True when every sample point sits within ``limit`` of the segment list.

    Vectorised: point-to-segment distance as an (N samples x M segments)
    matrix, reduced per sample. ``segs`` is (M, 4) — a, b xy per row.
    """
    if len(segs) == 0:
        return False
    arr = np.asarray(segs, dtype=float)
    if arr.ndim != 2 or arr.shape[1] != 4:
        arr = arr.reshape(-1, 4) if arr.size and isinstance(arr[0], (list, tuple)) else arr
    ax, ay, bx, by = arr.T
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    denom = np.where(denom <= 0.0, 1.0, denom)
    px = samples[:, 0][:, None]  # (N, 1)
    py = samples[:, 1][:, None]
    ux = px - ax                 # (N, M)
    uy = py - ay
    t = np.clip((ux * dx + uy * dy) / denom, 0.0, 1.0)
    qx = ax + t * dx
    qy = ay + t * dy
    d = np.hypot(px - qx, py - qy)  # broadcast to (N, M)
    return bool(d.min(axis=1).max() < limit)


def corridor_keep(strokes: list, width: float, *, tol_frac: float = CORRIDOR_TOL_FRAC,
                  split: int = CORRIDOR_SPLIT) -> list[int]:
    """Indices of the strokes worth drawing, in input order.

    ``strokes`` are canvas-unit polylines of ONE layer (layer-local collapse);
    ``width`` is the canvas width the tolerance fraction resolves against.
    Degenerate strokes (single points) are always kept — the symmetric check
    keeps everything else only when it really retraces.
    """
    n = len(strokes)
    if n < 2:
        return list(range(n))
    tol = tol_frac * width
    step = tol * _SAMPLE_STEP_FRAC
    kept: list[int] = []
    reps: list[tuple[np.ndarray, tuple[float, float, float, float], np.ndarray]] = []
    joins: list[int] = []
    for i, pl in enumerate(strokes):
        if len(pl) < 2:
            kept.append(i)
            continue
        segs = np.asarray([(a[0], a[1], b[0], b[1]) for a, b in zip(pl, pl[1:])], dtype=float)
        x0 = min(p[0] for p in pl); y0 = min(p[1] for p in pl)
        x1 = max(p[0] for p in pl); y1 = max(p[1] for p in pl)
        samples = _samples(pl, step)
        placed = False
        for j in range(len(reps) - 1, -1, -1):
            if joins[j] >= split:
                continue
            rsegs, rb, rsamples = reps[j]
            if (x1 < rb[0] or x0 > rb[1] or y1 < rb[2] or y0 > rb[3]):
                continue
            if _directed(samples, rsegs, tol) and _directed(rsamples, segs, tol):
                joins[j] += 1
                placed = True
                break
        if not placed:
            kept.append(i)
            reps.append((segs, (x0 - tol, x1 + tol, y0 - tol, y1 + tol), samples))
            joins.append(0)
    return kept


def collapse_counts(strokes: list, width: float, **kw) -> int:
    """How many strokes survive the collapse (convenience for tests/stats)."""
    return len(corridor_keep(strokes, width, **kw))
