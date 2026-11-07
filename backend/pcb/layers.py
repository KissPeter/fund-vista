"""Gerber and Excellon objects to per-sub-layer shapes, and one SVG group each.

gerbonara objects give the split (plan 4.2, geometry fallback): a flash is a
pad, a line or arc is a track, a region is a pour, and every Excellon object
is a drill. Shapes are in board millimetres, Y up, before any fit to paper.
Clear-polarity objects are dropped: the outline output has no knock-out.

``board_shapes`` is the child-process parse target (see ``backend.pcb.render``
``run_isolated``), so it returns plain picklable data.
"""

from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from gerbonara import ExcellonFile, GerberFile
from gerbonara import graphic_primitives as gp
from gerbonara.graphic_objects import Arc, Flash, Line, Region
from gerbonara.utils import MM

from backend.pcb.render import _sniff
from backend.penplot.errors import ErrorCode, PenPlotError

#: Sub-layer ids, in emission order. They are also the ``data-kp-layer`` values.
SUBLAYER_IDS: tuple[str, ...] = ("pads", "tracks", "pours", "drills")

#: Parser limit: objects per file, so one small file cannot expand into a
#: huge path set (plan section 6, S1 row "Parser limits").
MAX_SHAPES = 50_000
#: Arc sampling: one vertex per ARC_STEP_MM of arc length, capped per arc.
ARC_STEP_MM = 0.05
ARC_MAX_SEGMENTS = 512
#: Pen width of every stroke in the SVG output (mm).
STROKE_WIDTH_MM = 0.25

Point = tuple[float, float]
Transform = Callable[[Point], Point]


@dataclass(frozen=True)
class Shape:
    """One drawable piece: a closed fill outline or an open centre-line stroke."""

    layer: str
    points: tuple[Point, ...]
    closed: bool


def _open_board(data: bytes):
    """Sniff ``data`` and parse it from a temp file removed in ``finally``."""
    kind = _sniff(data)
    fd, path = tempfile.mkstemp(suffix=".gbr")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if kind == "excellon":
            return kind, ExcellonFile.open(path)
        return kind, GerberFile.open(path)
    finally:
        os.unlink(path)


def _clean(points: list[Point]) -> list[Point]:
    """Drop consecutive duplicate vertices and a repeated closing vertex."""
    out: list[Point] = []
    for point in points:
        if not out or point != out[-1]:
            out.append((float(point[0]), float(point[1])))
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def _arc_points(
    x1: float, y1: float, x2: float, y2: float,
    cx: float, cy: float, clockwise: bool,
) -> list[Point]:
    """Sample a circular arc from (x1, y1) to (x2, y2) about (cx, cy)."""
    radius = math.hypot(x1 - cx, y1 - cy)
    a1 = math.atan2(y1 - cy, x1 - cx)
    a2 = math.atan2(y2 - cy, x2 - cx)
    if clockwise:
        sweep, sign = (a1 - a2) % math.tau, -1.0
    else:
        sweep, sign = (a2 - a1) % math.tau, 1.0
    if math.isclose(x1, x2, abs_tol=1e-9) and math.isclose(y1, y2, abs_tol=1e-9):
        sweep = math.tau  # a full circle starts and ends at one point
    steps = min(ARC_MAX_SEGMENTS, max(8, math.ceil(sweep * radius / ARC_STEP_MM)))
    points = [
        (cx + radius * math.cos(a1 + sign * sweep * i / steps),
         cy + radius * math.sin(a1 + sign * sweep * i / steps))
        for i in range(steps + 1)
    ]
    points[0] = (x1, y1)
    points[-1] = (x2, y2)
    return points


def _stroke_points(prim) -> list[Point]:
    """Centre-line vertices of a line or arc primitive (tracks, routes)."""
    if isinstance(prim, gp.Line):
        return [(prim.x1, prim.y1), (prim.x2, prim.y2)]
    if isinstance(prim, gp.Arc):
        return _arc_points(prim.x1, prim.y1, prim.x2, prim.y2, prim.cx, prim.cy, prim.clockwise)
    return []


def _outline_points(prim) -> list[Point]:
    """Closed outline vertices of a fill primitive (pad, pour, drill hole)."""
    outline = prim.to_arc_poly().approximate_arcs().outline
    return [(float(x), float(y)) for x, y in outline]


def _object_shapes(obj, excellon: bool) -> list[Shape]:
    if not getattr(obj, "polarity_dark", True):
        return []
    if isinstance(obj, Region):
        layer, closed = "pours", True
    elif isinstance(obj, Flash):
        layer, closed = ("drills" if excellon else "pads"), True
    elif isinstance(obj, (Line, Arc)):
        layer, closed = ("drills" if excellon else "tracks"), False
    else:
        return []
    shapes: list[Shape] = []
    for prim in obj.to_primitives(unit=MM):
        points = _outline_points(prim) if closed else _stroke_points(prim)
        points = _clean(points)
        if len(points) >= (3 if closed else 2):
            shapes.append(Shape(layer=layer, points=tuple(points), closed=closed))
    return shapes


def board_shapes(data: bytes) -> list[Shape]:
    """Parse one Gerber or Excellon file into sub-layer shapes (mm, Y up)."""
    kind, board = _open_board(data)
    excellon = kind == "excellon"
    shapes: list[Shape] = []
    for obj in board.objects:
        shapes.extend(_object_shapes(obj, excellon))
        if len(shapes) > MAX_SHAPES:
            raise PenPlotError(
                status=422,
                code=ErrorCode.INVALID_PARAMS,
                message=f"The file has more than {MAX_SHAPES} drawn objects; it is too large to plot.",
            )
    return shapes


def _flip_y(point: Point) -> Point:
    return (point[0], -point[1])


def _path_d(points: list[Point], closed: bool) -> str:
    head = f"M{points[0][0]:.3f} {points[0][1]:.3f}"
    body = " ".join(f"L{x:.3f} {y:.3f}" for x, y in points[1:])
    return f"{head} {body}{' Z' if closed else ''}".rstrip()


def sublayer_groups(
    shapes: list[Shape],
    *,
    place: Transform = _flip_y,
    inks: Mapping[str, str] | None = None,
    outline: bool = False,
) -> dict[str, str]:
    """One SVG ``<g>`` per sub-layer present in ``shapes``, keyed by sub-layer id.

    Each group is ``id="pcb-<layer>"`` and carries ``data-kp-layer``. Its
    stroke is the layer's ink (``inks``, default black). Closed shapes are
    filled, or with ``outline=True`` drawn as their outline stroke only (the
    fill-to-outline rule of plan D4). Layers with no shapes are absent.
    """
    inks = inks or {}
    groups: dict[str, str] = {}
    for layer in SUBLAYER_IDS:
        members = [shape for shape in shapes if shape.layer == layer]
        if not members:
            continue
        ink = inks.get(layer, "black")
        tag = (
            f'<g id="pcb-{layer}" data-kp-layer="{layer}" stroke="{ink}" fill="none" '
            f'stroke-width="{STROKE_WIDTH_MM}" stroke-linecap="round" stroke-linejoin="round">'
        )
        paths: list[str] = []
        for shape in members:
            points = [place(point) for point in shape.points]
            if shape.closed and not outline:
                paths.append(f'<path d="{_path_d(points, True)}" fill="{ink}" stroke="none"/>')
            else:
                paths.append(f'<path d="{_path_d(points, shape.closed)}"/>')
        groups[layer] = "\n".join([tag, *paths, "</g>"])
    return groups
