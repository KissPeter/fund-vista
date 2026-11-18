"""Stop each track at the edge of the pad it runs into.

A pad is plotted as its outline, so a track that runs on to the pad centre
draws a stub inside the pad ring. Each track is cut where it crosses a pad
outline, and the parts inside a pad are dropped; the parts outside stay as
open polylines. This is the same rule the airport render uses to stop a
taxiway at the edge of a runway (``backend.airports.render._cut_polyline``).

Pure geometry, no I/O. ``compose_sheet`` applies it only when both the pads
and the tracks sub-layers are plotted: trimming tracks to pads that are not
drawn would leave gaps for nothing.
"""

from __future__ import annotations

import math
from collections import defaultdict

from backend.pcb.layers import Point, Shape

#: Grid cell size (mm) of the pad index: a track only tests pads near it.
CELL_MM = 2.0


def _cell(value: float) -> int:
    return math.floor(value / CELL_MM)


def _inside(point: Point, poly: list[Point]) -> bool:
    """Even-odd ray test; ``poly`` is closed implicitly (last vertex joins the first)."""
    x, y = point
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < xi + (y - yi) * (xj - xi) / (yj - yi):
            inside = not inside
        j = i
    return inside


def _crossings(p0: Point, p1: Point, poly: list[Point]) -> list[float]:
    """Parameters t in [0, 1] where segment p0-p1 crosses an edge of ``poly``."""
    x0, y0 = p0
    dx, dy = p1[0] - x0, p1[1] - y0
    ts: list[float] = []
    for i in range(len(poly)):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % len(poly)]
        ex, ey = bx - ax, by - ay
        denom = dx * ey - dy * ex
        if denom == 0:  # parallel: no single crossing point
            continue
        wx, wy = ax - x0, ay - y0
        t = (wx * ey - wy * ex) / denom
        u = (wx * dy - wy * dx) / denom
        if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
            ts.append(t)
    return ts


def _at(p0: Point, p1: Point, t: float) -> Point:
    if t == 0.0:
        return p0
    if t == 1.0:
        return p1
    return (p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t)


class _PadIndex:
    """Uniform grid over the pad outlines, keyed by the cells each bbox covers."""

    def __init__(self, pads: list[list[Point]]) -> None:
        self.pads = pads
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        for k, poly in enumerate(pads):
            xs = [x for x, _ in poly]
            ys = [y for _, y in poly]
            for cx in range(_cell(min(xs)), _cell(max(xs)) + 1):
                for cy in range(_cell(min(ys)), _cell(max(ys)) + 1):
                    self.cells[(cx, cy)].append(k)

    def near(self, p0: Point, p1: Point) -> list[int]:
        """Indices of the pads whose cells the segment's bbox covers (a superset)."""
        x_lo, x_hi = sorted((p0[0], p1[0]))
        y_lo, y_hi = sorted((p0[1], p1[1]))
        found: set[int] = set()
        for cx in range(_cell(x_lo), _cell(x_hi) + 1):
            for cy in range(_cell(y_lo), _cell(y_hi) + 1):
                found.update(self.cells.get((cx, cy), ()))
        return sorted(found)


def _trim_track(points: list[Point], index: _PadIndex) -> list[list[Point]]:
    """The parts of one open polyline that lie outside every pad, as polylines."""
    out: list[list[Point]] = []
    cur: list[Point] = []
    for p0, p1 in zip(points, points[1:]):
        near = index.near(p0, p1)
        ts = {0.0, 1.0}
        for k in near:
            ts.update(_crossings(p0, p1, index.pads[k]))
        bounds = sorted(ts)
        for ta, tb in zip(bounds, bounds[1:]):
            if tb <= ta:
                continue
            mid = _at(p0, p1, (ta + tb) / 2)
            start, end = _at(p0, p1, ta), _at(p0, p1, tb)
            if any(_inside(mid, index.pads[k]) for k in near):
                if len(cur) > 1:
                    out.append(cur)
                cur = []
            else:
                if not cur:
                    cur = [start]
                cur.append(end)
    if len(cur) > 1:
        out.append(cur)
    return out


def trim_tracks_at_pads(shapes: list[Shape]) -> list[Shape]:
    """Cut every open track at the outline of the pads it runs into.

    Closed shapes and tracks that touch no pad are returned unchanged. A track
    that ends on a pad centre stops on the pad outline; a track crossing a pad
    is split around it. Order of the shapes is kept.
    """
    pads = [list(shape.points) for shape in shapes if shape.layer == "pads" and shape.closed]
    if not pads:
        return list(shapes)
    index = _PadIndex(pads)
    out: list[Shape] = []
    for shape in shapes:
        if shape.layer != "tracks" or shape.closed:
            out.append(shape)
            continue
        for piece in _trim_track(list(shape.points), index):
            out.append(Shape(layer="tracks", points=tuple(piece), closed=False))
    return out
