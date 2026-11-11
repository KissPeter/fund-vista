"""Stroke lettering for the weather artwork, with a collision pass.

Lettering reuses the plotter faces of :mod:`backend.penplot.labels` (the same
single-stroke font the shop uses for title labels); no font is added here and
no ``<text>`` is ever emitted. Boxes are in sheet millimetres, y down.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from backend.penplot.labels import _glyph_strokes
from backend.weather.config import LABEL_FONT, LABEL_PAD_MM

Polyline = list[tuple[float, float]]


@dataclass(frozen=True)
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    def overlaps(self, other: "Box", pad: float = 0.0) -> bool:
        return not (
            self.x1 + pad <= other.x0 or other.x1 + pad <= self.x0
            or self.y1 + pad <= other.y0 or other.y1 + pad <= self.y0
        )

    def inside(self, outer: "Box") -> bool:
        return (self.x0 >= outer.x0 and self.x1 <= outer.x1
                and self.y0 >= outer.y0 and self.y1 <= outer.y1)


@dataclass(frozen=True)
class Lettering:
    """One line of stroke text: strokes with their origin at the box's top-left."""

    strokes: list[Polyline]
    width: float
    height: float
    missing: tuple[str, ...]

    def box_at(self, x0: float, y0: float, pad: float = LABEL_PAD_MM) -> Box:
        return Box(x0 - pad, y0 - pad, x0 + self.width + pad, y0 + self.height + pad)

    def placed(self, x0: float, y0: float) -> list[Polyline]:
        return [[(x + x0, y + y0) for x, y in s] for s in self.strokes]


def lettering(text: str, height_mm: float) -> Lettering:
    """Stroke lettering for ``text`` at a cap height of ``height_mm``."""
    raw, _advance, missing = _glyph_strokes(text, height_mm, LABEL_FONT)
    if not raw:
        return Lettering(strokes=[], width=0.0, height=0.0, missing=tuple(missing))
    xs = [x for s in raw for x, _ in s]
    ys = [y for s in raw for _, y in s]
    mx, my = min(xs), min(ys)
    strokes = [[(x - mx, y - my) for x, y in s] for s in raw]
    return Lettering(
        strokes=strokes,
        width=max(xs) - mx,
        height=max(ys) - my,
        missing=tuple(missing),
    )


def _hits_points(box: Box, points: np.ndarray) -> bool:
    px, py = points[:, 0], points[:, 1]
    return bool(np.any((px >= box.x0) & (px <= box.x1) & (py >= box.y0) & (py <= box.y1)))


def choose_slot(
    text: Lettering,
    anchor: tuple[float, float, float, float],
    taken: list[Box],
    frame: Box,
    *,
    gap: float,
    obstacles: np.ndarray | None = None,
) -> Box | None:
    """First free box around ``anchor`` for ``text``, or None.

    ``anchor`` is the (x0, y0, x1, y1) of the point being labelled (a dot or a
    marker letter). Candidates go right, left, above, below, then the four
    diagonals; a candidate must sit inside ``frame``, clear every box in
    ``taken`` and, when given, hold no ``obstacles`` point (an (n, 2) array of
    paper points such as coastline vertices).
    """
    ax0, ay0, ax1, ay1 = anchor
    cx = (ax0 + ax1) / 2.0
    cy = (ay0 + ay1) / 2.0
    w = text.width + 2 * LABEL_PAD_MM
    h = text.height + 2 * LABEL_PAD_MM
    candidates = [
        (ax1 + gap, cy - h / 2),                 # right
        (ax0 - gap - w, cy - h / 2),             # left
        (cx - w / 2, ay0 - gap - h),             # above
        (cx - w / 2, ay1 + gap),                 # below
        (ax1 + gap, ay0 - gap - h),              # upper right
        (ax1 + gap, ay1 + gap),                  # lower right
        (ax0 - gap - w, ay0 - gap - h),          # upper left
        (ax0 - gap - w, ay1 + gap),              # lower left
    ]
    for bx, by in candidates:
        box = Box(bx, by, bx + w, by + h)
        if not box.inside(frame):
            continue
        if any(box.overlaps(t) for t in taken):
            continue
        if obstacles is not None and obstacles.size and _hits_points(box, obstacles):
            continue
        return box
    return None
