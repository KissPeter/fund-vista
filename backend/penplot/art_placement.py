"""Art placement around the gallery context table (gallery sheet only).

Python port of the shop's ``artInk.ts`` (ink mask) and ``artPlacement.ts``
(placement), specified in the shop's ``docs/table-drawing-overlap-plan.md``.
The gallery sheet (``compose_sheet.py``) uses it so the artwork never sits
under the context table's real linework. The custom designer path
(``pipeline.py``, ``knockout.py``) is a different mechanism and does not use
this module.

Geometry is in sheet mm. Rects are ``(x, y, w, h)`` tuples. The ink mask is a
coarse occupancy grid over the artwork: cell (c, r) covers
``[c/cols, (c+1)/cols) x [r/rows, (r+1)/rows)`` of the artwork, and because the
artwork is aspect-fit into its box the mask maps linearly onto the content
rect (the viewport fitted and centred in the box), not onto the whole box.

Placement, first success wins:

1. No table, or no ink within ``gap_mm`` of it: the box is returned unchanged.
2. Slide the box away from the table along an axis with slack, in 1 mm steps
   (the exact slack is the last step); the smallest shift that clears wins.
3. Shrink the box toward the area corner opposite the table, in 0.02 scale
   steps down to ``min_scale``; the largest clearing scale wins.
4. Otherwise the original box with ``overlap=True``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import chain
from typing import Sequence

import numpy as np

#: Axis-aligned rectangle ``(x, y, w, h)`` in sheet mm.
Rect = tuple[float, float, float, float]

#: Default mask resolution along the artwork width (the plan's ``cols = 64``).
DEFAULT_COLS = 64
#: Clearance kept between the art ink and the table, in mm (the plan's gap).
DEFAULT_GAP_MM = 2.0
#: Smallest scale the artwork may shrink to (the plan's ``minScale``).
DEFAULT_MIN_SCALE = 0.8
#: Shrink step between candidate scales (the plan's 0.02).
SCALE_STEP = 0.02
#: Upper bound on sampled points per numpy chunk (keeps peak memory flat).
_CHUNK_SAMPLES = 1 << 20


@dataclass(frozen=True)
class InkMask:
    """Coarse ink occupancy: ``cells`` is row-major, ``rows * cols`` booleans.

    ``aspect`` is the artwork viewport's width / height. The `<image>` draws
    the viewport aspect-fitted (centred, ``meet``) inside its box, so the mask
    maps onto that content rect, not onto the box itself. ``None`` means the
    mask fills the box it is mapped onto.
    """

    cols: int
    rows: int
    cells: np.ndarray
    aspect: float | None = None


def content_rect(box: Rect, aspect: float) -> Rect:
    """The viewport of aspect ``aspect`` fitted and centred inside ``box``.

    Mirrors the SVG default ``preserveAspectRatio="xMidYMid meet"`` that the
    `<image>` applies to the artwork inside its box.
    """
    bx, by, bw, bh = box
    if bw / bh > aspect:
        w = bh * aspect
        return (bx + (bw - w) / 2, by, w, bh)
    h = bw / aspect
    return (bx, by + (bh - h) / 2, bw, h)


@dataclass(frozen=True)
class ArtPlacement:
    """Where the artwork ends up, and how far it moved and shrank."""

    box: Rect
    dx: float
    dy: float
    scale: float
    overlap: bool


def _js_round(value: float) -> int:
    """``Math.round`` for the non-negative values used here (half rounds up)."""
    return math.floor(value + 0.5)


def ink_mask(
    polylines: Sequence[Sequence[tuple[float, float]]],
    width_px: float,
    height_px: float,
    cols: int = DEFAULT_COLS,
) -> InkMask | None:
    """Rasterise artwork polylines into a dilated ink mask.

    ``polylines`` are in viewport-px space (as returned by
    ``svg_vector.parse_svg_vectors``); ``width_px``/``height_px`` are that
    viewport. Every segment is walked at half-cell steps so no gaps show along
    long lines, then the mask is dilated by one cell (8-neighbourhood).
    Returns ``None`` when the viewport is degenerate. The work is vectorised,
    so it is O(segments) in numpy rather than a Python loop per sample.
    """
    if not (width_px > 0 and height_px > 0) or cols < 1:
        return None
    cols = int(cols)
    rows = max(1, _js_round(cols * height_px / width_px))
    scale_x = cols / width_px
    scale_y = rows / height_px
    cells = np.zeros(rows * cols, dtype=bool)

    lengths = np.fromiter((len(pl) for pl in polylines), dtype=np.int64, count=len(polylines))
    lengths = lengths[lengths > 0]
    total = int(lengths.sum())
    aspect = width_px / height_px
    if total == 0:
        return InkMask(cols, rows, cells, aspect)
    pts = np.fromiter(
        chain.from_iterable(chain.from_iterable(polylines)),
        dtype=float, count=2 * total,
    ).reshape(-1, 2)
    pts[:, 0] *= scale_x
    pts[:, 1] *= scale_y
    ends = np.cumsum(lengths)
    starts = ends - lengths

    # One-point polylines are a single ink cell; longer ones contribute the
    # segments between consecutive points that belong to the same polyline.
    singles = pts[starts[lengths == 1]]
    singles = singles[np.isfinite(singles).all(axis=1)]
    seg_ok = np.ones(total - 1, dtype=bool)
    seg_ok[ends[:-1] - 1] = False
    seg_a = pts[:-1][seg_ok]
    seg_b = pts[1:][seg_ok]
    finite = np.isfinite(seg_a).all(axis=1) & np.isfinite(seg_b).all(axis=1)
    seg_a, seg_b = seg_a[finite], seg_b[finite]
    ax, ay = seg_a[:, 0], seg_a[:, 1]
    bx, by = seg_b[:, 0], seg_b[:, 1]
    # Drop segments wholly outside the grid on one axis. The far edge counts as
    # inside: ink on the viewport's right or bottom edge sits at cols / rows and
    # clips into the last cell, so a full-bleed edge must still be inked.
    visible = ~(
        (np.maximum(ax, bx) < 0) | (np.minimum(ax, bx) > cols)
        | (np.maximum(ay, by) < 0) | (np.minimum(ay, by) > rows)
    )
    ax, ay, bx, by = ax[visible], ay[visible], bx[visible], by[visible]
    steps = np.minimum(
        4 * (cols + rows),
        np.ceil(np.maximum(np.abs(bx - ax), np.abs(by - ay)) * 2),
    ).astype(np.int64)

    def mark(u: np.ndarray, v: np.ndarray) -> None:
        c = np.clip(np.floor(u), 0, cols - 1).astype(np.int64)
        r = np.clip(np.floor(v), 0, rows - 1).astype(np.int64)
        cells[r * cols + c] = True

    if singles.size:
        mark(singles[:, 0], singles[:, 1])
    # Sample each segment at (steps + 1) points, in bounded numpy chunks.
    counts = steps + 1
    cum = np.cumsum(counts)
    n = len(steps)
    first = 0
    while first < n:
        base = int(cum[first - 1]) if first else 0
        last = int(np.searchsorted(cum, base + _CHUNK_SAMPLES, side="right"))
        last = min(max(last, first + 1), n)
        c_ = counts[first:last]
        st = np.repeat(steps[first:last], c_)
        offs = np.repeat(np.cumsum(c_) - c_, c_)
        s = np.arange(int(c_.sum())) - offs
        t = np.where(st == 0, 0.0, s / np.maximum(st, 1))
        ax_r = np.repeat(ax[first:last], c_)
        ay_r = np.repeat(ay[first:last], c_)
        mark(
            ax_r + (np.repeat(bx[first:last], c_) - ax_r) * t,
            ay_r + (np.repeat(by[first:last], c_) - ay_r) * t,
        )
        first = last

    # Dilate by one cell, 8-neighbourhood: OR of the nine shifted copies.
    grid = cells.reshape(rows, cols)
    padded = np.pad(grid, 1)
    out = np.zeros_like(grid)
    for dr in range(3):
        for dc in range(3):
            out |= padded[dr : dr + rows, dc : dc + cols]
    return InkMask(cols, rows, out.reshape(-1), aspect)


def ink_overlaps_rect(
    mask: InkMask | None, box: Rect, rect: Rect, gap_mm: float = DEFAULT_GAP_MM
) -> bool:
    """True when ink mapped onto ``box`` reaches ``rect`` grown by ``gap_mm``.

    The mask maps onto the content rect of ``box`` (the viewport aspect-fitted
    inside it, see :func:`content_rect`). A ``None`` mask means the whole box
    is ink.
    """
    x0 = rect[0] - gap_mm
    x1 = rect[0] + rect[2] + gap_mm
    y0 = rect[1] - gap_mm
    y1 = rect[1] + rect[3] + gap_mm
    if mask is None:
        bx, by, bw, bh = box
        return bx < x1 and bx + bw > x0 and by < y1 and by + bh > y0
    if mask.aspect is not None:
        box = content_rect(box, mask.aspect)
    bx, by, bw, bh = box
    rr, cc = np.nonzero(mask.cells.reshape(mask.rows, mask.cols))
    if rr.size == 0:
        return False
    cx0 = bx + (cc * bw) / mask.cols
    cx1 = bx + ((cc + 1) * bw) / mask.cols
    cy0 = by + (rr * bh) / mask.rows
    cy1 = by + ((rr + 1) * bh) / mask.rows
    hit = (cx0 < x1) & (cx1 > x0) & (cy0 < y1) & (cy1 > y0)
    return bool(hit.any())


def _shift_steps(max_mm: float) -> list[float]:
    """1 mm steps up to ``max_mm``, always ending exactly on ``max_mm``."""
    out: list[float] = []
    s = 1
    while s < max_mm:
        out.append(float(s))
        s += 1
    if max_mm > 0:
        out.append(float(max_mm))
    return out


def place_art_around_table(
    area: Rect,
    box: Rect,
    mask: InkMask | None,
    table: Rect | None,
    gap_mm: float = DEFAULT_GAP_MM,
    min_scale: float = DEFAULT_MIN_SCALE,
) -> ArtPlacement:
    """Place the art box so its real ink clears the table (see module docs).

    ``area`` is the free area the art must stay inside; ``box`` is the
    aspect-fit art box before avoidance; ``mask`` is its ink (``None`` = the
    whole box is ink); ``table`` is the table's outer box (``None`` = no table).
    """
    unchanged = ArtPlacement(box, 0.0, 0.0, 1.0, False)
    if table is None or not ink_overlaps_rect(mask, box, table, gap_mm):
        return unchanged

    ax, ay, aw, ah = area
    bx, by, bw, bh = box
    tx, ty, tw, th = table
    # Direction the art travels to leave the table on each axis: +1 / -1 when
    # the table is on the left / top half (right / bottom), 0 when centred.
    area_cx = ax + aw / 2
    area_cy = ay + ah / 2
    table_cx = tx + tw / 2
    table_cy = ty + th / 2
    dir_x = 1 if table_cx < area_cx else -1 if table_cx > area_cx else 0
    dir_y = 1 if table_cy < area_cy else -1 if table_cy > area_cy else 0

    # Slide: candidate shifts with slack on each axis, smallest first (ties
    # keep the x candidates before the y ones, as the shop's stable sort does).
    if dir_x > 0:
        slack_x = ax + aw - (bx + bw)
    elif dir_x < 0:
        slack_x = bx - ax
    else:
        slack_x = 0.0
    if dir_y > 0:
        slack_y = ay + ah - (by + bh)
    elif dir_y < 0:
        slack_y = by - ay
    else:
        slack_y = 0.0
    slides: list[tuple[float, float, float]] = [
        *((mm, dir_x * mm, 0.0) for mm in _shift_steps(max(0.0, slack_x))),
        *((mm, 0.0, dir_y * mm) for mm in _shift_steps(max(0.0, slack_y))),
    ]
    slides.sort(key=lambda item: item[0])
    for _mm, dx, dy in slides:
        moved: Rect = (bx + dx, by + dy, bw, bh)
        if not ink_overlaps_rect(mask, moved, table, gap_mm):
            return ArtPlacement(moved, dx, dy, 1.0, False)

    # Shrink: anchor to the area corner opposite the table, centred on any
    # axis the table does not lean on.
    steps = math.floor((1 - min_scale) / SCALE_STEP + 1e-9)
    for k in range(1, steps + 1):
        scale = max(min_scale, 1 - k * SCALE_STEP)
        w = bw * scale
        h = bh * scale
        if dir_x > 0:
            x = ax + aw - w
        elif dir_x < 0:
            x = ax
        else:
            x = ax + (aw - w) / 2
        if dir_y > 0:
            y = ay + ah - h
        elif dir_y < 0:
            y = ay
        else:
            y = ay + (ah - h) / 2
        shrunk: Rect = (x, y, w, h)
        if not ink_overlaps_rect(mask, shrunk, table, gap_mm):
            return ArtPlacement(shrunk, x - bx, y - by, scale, False)

    return ArtPlacement(box, 0.0, 0.0, 1.0, True)
