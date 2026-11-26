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
is compared with the **union of recent accepted strokes** of each corridor,
not only with its first seed: a slowly drifting funnel stays one corridor and
a 90 % retrace with a diverging tail joins it, keeping just the uncovered tail
as extra ink. ``max_passes`` caps the pen passes of one corridor (10 by
default, one pass per ~20 movements), while distinct corridors still survive.

Pure geometry, layer-local: collapses only where strokes really retrace;
everywhere else nothing changes (owner decision 2026-10-27; pass cap
2026-11-26). The original POC in ``tools/airport_traffic/corridors.py`` has
the older seed-only pass; this backend copy is canonical for renders.
"""

from __future__ import annotations

import math

import numpy as np

#: Corridor tolerance as a fraction of the canvas width. At the default
#: 1000-wide canvas this is 2 user units; the sheet plots at 0.3 mm/unit, so
#: 2 units ≈ 0.6 mm — about two pen-widths apart, where separate passes only
#: over-ink the same line.
CORRIDOR_TOL_FRAC = 0.002

#: Hard cap on pen passes for one corridor in one layer. A busy funnel must
#: never re-ink the paper more than this many times, whatever the traffic.
CORRIDOR_MAX_PASSES = 10

#: Funnel size the pass cap is calibrated against: one pass per 20 movements
#: at the default cap, so a 50-flight funnel gets 3 passes, not 10.
CORRIDOR_REFERENCE_STROKES = 200

#: A stroke joins a corridor when this share of its samples lie within the
#: tolerance of the union of its recent members. Diverging tails below the
#: remainder are emitted as short extra passes instead of full duplicates.
CORRIDOR_COVERAGE = 0.85

#: Recent accepted geometries kept per corridor for the union comparison;
#: rolling anchors let a slowly drifting funnel stay a single corridor.
CORRIDOR_ANCHORS = 4

#: Resample step for the distance estimate, as a fraction of the tolerance.
#: At 0.25 the sampled Hausdorff stays within half a step (a quarter of the
#: tolerance) of the true one against the chord-broken polyline.
_SAMPLE_STEP_FRAC = 0.25

#: Coverage/tail detection needs a shape, not millimetre resolution: cap the
#: sample count so a dense day stays inside the render budget.
_SAMPLE_MAX = 64


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


def _segments(pl) -> np.ndarray:
    """Segment array (M, 4): ax, ay, bx, by per polyline edge."""
    return np.asarray([(a[0], a[1], b[0], b[1]) for a, b in zip(pl, pl[1:])], dtype=float).reshape(-1, 4)


def _bbox(samples: np.ndarray) -> tuple[float, float, float, float]:
    return (float(samples[:, 0].min()), float(samples[:, 0].max()),
            float(samples[:, 1].min()), float(samples[:, 1].max()))


def _poly_len(pl) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pl, pl[1:]))


def _thin(samples: np.ndarray, limit: int = _SAMPLE_MAX) -> np.ndarray:
    """Evenly spaced subset, keeping the endpoints, for coverage checks."""
    if len(samples) <= limit:
        return samples
    idx = np.linspace(0, len(samples) - 1, limit, dtype=int)
    return samples[idx]


def _overlaps(a: tuple, b: tuple, tol: float) -> bool:
    return not (a[1] < b[0] - tol or a[0] > b[1] + tol
                or a[3] < b[2] - tol or a[2] > b[3] + tol)


def _near_mask(samples: np.ndarray, segs: np.ndarray, limit: float) -> np.ndarray:
    """Boolean mask: each sample lies within ``limit`` of any segment."""
    if not len(segs):
        return np.zeros(len(samples), dtype=bool)
    arr = np.asarray(segs, dtype=float).reshape(-1, 4)
    ax, ay, bx, by = arr.T
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    denom = np.where(denom <= 0.0, 1.0, denom)
    px = samples[:, 0][:, None]
    py = samples[:, 1][:, None]
    ux, uy = px - ax, py - ay
    t = np.clip((ux * dx + uy * dy) / denom, 0.0, 1.0)
    d = np.hypot(px - (ax + t * dx), py - (ay + t * dy))
    return (d <= limit).any(axis=1)


def _tail_run(near: np.ndarray, samples: np.ndarray, min_len: float) -> tuple[int, int] | None:
    """Longest consecutive uncovered stretch, or None when nothing is worth ink."""
    best: tuple[int, int, float] | None = None
    i = 0
    while i < len(near):
        if near[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(near) and not near[j + 1]:
            j += 1
        if j > i:
            length = float(np.hypot(np.diff(samples[i:j + 1, 0]), np.diff(samples[i:j + 1, 1])).sum())
            if length >= min_len and (best is None or length > best[2]):
                best = (i, j, length)
        i = j + 1
    return None if best is None else (best[0], best[1])


def corridor_paths(strokes: list, width: float, *,
                   max_passes: int = CORRIDOR_MAX_PASSES,
                   tol_frac: float = CORRIDOR_TOL_FRAC,
                   coverage: float = CORRIDOR_COVERAGE,
                   anchors: int = CORRIDOR_ANCHORS) -> list[tuple[int, list]]:
    """``(index, polyline)`` pen passes worth drawing, in input order.

    ``strokes`` are canvas-unit polylines of ONE layer; ``width`` resolves the
    tolerance fraction. Degenerate strokes are always kept. A stroke joins the
    closest corridor whose rolling anchor union covers ``coverage`` of its
    samples; its uncovered tail, when long enough, is kept as a short extra
    pass. Each corridor contributes at most ``max_passes`` full passes, chosen
    evenly across its members so density shows without re-inking one spot.
    """
    cap = max(1, int(max_passes))
    if not strokes:
        return []
    tol = tol_frac * width
    step = tol * _SAMPLE_STEP_FRAC
    stride = max(1, CORRIDOR_REFERENCE_STROKES // cap)
    prepared: list[tuple[list, np.ndarray | None, np.ndarray | None, tuple | None]] = []
    for pl in strokes:
        if len(pl) < 2 or _poly_len(pl) <= 0.5 * tol:
            prepared.append((pl, None, None, None))  # sub-pen stub: never merged
            continue
        samples = _thin(_samples(pl, step))
        prepared.append((pl, samples, _segments(pl), _bbox(samples)))

    clusters: list[dict] = []
    singles: list[tuple[int, list]] = []
    for idx, (pl, samples, segs, bb) in enumerate(prepared):
        if samples is None:
            singles.append((idx, pl))
            continue
        best: tuple[float, dict, np.ndarray] | None = None
        for cluster in clusters:
            if not _overlaps(bb, cluster["bbox"], tol):
                continue
            # One vectorised pass over the whole rolling union: four separate
            # mask calls per candidate cost 4x the Python overhead on a dense
            # day, which blew the 15 s render budget.
            near = _near_mask(samples, cluster["union"], tol)
            share = float(near.mean())
            if share >= coverage and (best is None or share > best[0]):
                best = (share, cluster, near)
        if best is None:
            clusters.append({"members": [idx], "anchors": [(segs, bb)],
                             "union": segs, "bbox": bb, "tails": []})
            continue
        _share, cluster, near = best
        cluster["members"].append(idx)
        cluster["anchors"].append((segs, bb))
        if len(cluster["anchors"]) > anchors:
            cluster["anchors"].pop(0)
        cluster["union"] = np.vstack([a[0] for a in cluster["anchors"]])
        cluster["bbox"] = (min(bb[0], cluster["bbox"][0]), max(bb[1], cluster["bbox"][1]),
                           min(bb[2], cluster["bbox"][2]), max(bb[3], cluster["bbox"][3]))
        run = _tail_run(near, samples, 3.0 * tol)
        if run and len(cluster["tails"]) < 2:
            cluster["tails"].append((idx, [tuple(float(v) for v in p) for p in samples[run[0]:run[1] + 1]]))

    drawn: list[tuple[int, list]] = []
    for cluster in clusters:
        members = cluster["members"]
        target = min(cap, math.ceil(len(members) / stride))
        if target == 1:
            chosen = [members[0]]  # the first (usually longest) stroke sets the pass
        else:
            chosen = [members[round(k * (len(members) - 1) / (target - 1))] for k in range(target)]
        chosen_set = set(chosen)
        drawn += [(i, strokes[i]) for i in chosen]
        drawn += [(i, tail) for i, tail in cluster["tails"] if i not in chosen_set]
    drawn += singles
    drawn.sort(key=lambda item: item[0])
    return drawn


def corridor_keep(strokes: list, width: float, *,
                  max_passes: int = CORRIDOR_MAX_PASSES, **kwargs) -> list[int]:
    """Indices of the strokes worth drawing, for callers that need no tail."""
    return [i for i, _pl in corridor_paths(strokes, width, max_passes=max_passes, **kwargs)]


def collapse_counts(strokes: list, width: float, **kwargs) -> int:
    """How many full passes survive the collapse (convenience for tests/stats)."""
    return len(corridor_keep(strokes, width, **kwargs))
