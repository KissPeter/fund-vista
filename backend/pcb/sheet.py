"""Fit a parsed board to one A4 sheet and plot its enabled sub-layers.

Pure geometry: no HTTP, no cache. The board's bounding box covers every
sub-layer of the file (enabled or not), so turning a layer off does not move
the artwork. Closed shapes are drawn as outline strokes (plan D4); tracks are
strokes already. The stroke colour is the resolved pen ink.

Stats come from the same helpers as the convert pipeline: ``polyline_length``
and ``dist`` (``backend.penplot.stages._grid``) and ``estimate_plot_time_s``
(``backend.penplot.pipeline``) with the default pen lift.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.fulfillment.ink_split import INK_HEX
from backend.pcb.layers import SUBLAYER_IDS, Point, Shape, sublayer_groups
from backend.pcb.schemas import LayerOut, PcbRenderParams, SheetStats
from backend.penplot.errors import ErrorCode, PenPlotError
from backend.penplot.pipeline import estimate_plot_time_s
from backend.penplot.schemas import PenParams
from backend.penplot.stages._grid import dist, polyline_length

A4_W_MM = 210.0
A4_H_MM = 297.0
MARGIN_MM = 10.0
#: Hard plot ceiling (plan 4.6, D6): reject above these, no manual quote path.
PEN_DOWN_LIMIT_MM = 20_000.0
PLOT_TIME_LIMIT_S = 1800.0


@dataclass(frozen=True)
class SheetResult:
    svg: str
    stats: SheetStats
    layers: list[LayerOut]
    rotated: bool
    warnings: list[str]


def _bounds(shapes: list[Shape]) -> tuple[float, float, float, float]:
    xs = [x for shape in shapes for x, _ in shape.points]
    ys = [y for shape in shapes for _, y in shape.points]
    return min(xs), min(ys), max(xs), max(ys)


def _ceiling_message(pen_down: float, est: float) -> str:
    causes: list[str] = []
    if pen_down > PEN_DOWN_LIMIT_MM:
        causes.append(f"{pen_down / 1000:.1f} m of pen-down (limit 20 m)")
    if est > PLOT_TIME_LIMIT_S:
        causes.append(f"about {est / 60:.0f} min to plot (limit 30 min)")
    return (
        "The board is too large to plot: " + " and ".join(causes)
        + ". Turn off sub-layers such as pours or drills to plot less."
    )


def compose_sheet(shapes: list[Shape], params: PcbRenderParams) -> SheetResult:
    """Lay the enabled sub-layers of ``shapes`` on one A4 sheet as an SVG."""
    enabled = params.enabled_layers()
    if not enabled:
        raise PenPlotError(
            status=422, code=ErrorCode.INVALID_PARAMS,
            message="Turn on at least one sub-layer to plot.",
        )
    if not shapes:
        raise PenPlotError(
            status=422, code=ErrorCode.INVALID_PARAMS,
            message="The file has no drawn objects to plot.",
        )
    xmin, ymin, xmax, ymax = _bounds(shapes)
    width, height = xmax - xmin, ymax - ymin
    if width <= 0 or height <= 0:
        raise PenPlotError(
            status=422, code=ErrorCode.INVALID_PARAMS,
            message="The board has no area to plot.",
        )

    avail_w, avail_h = A4_W_MM - 2 * MARGIN_MM, A4_H_MM - 2 * MARGIN_MM
    plain_scale = min(avail_w / width, avail_h / height)
    turned_scale = min(avail_w / height, avail_h / width)
    rotated = turned_scale > plain_scale
    scale = turned_scale if rotated else plain_scale
    span_u = (height if rotated else width) * scale
    span_v = (width if rotated else height) * scale
    offset_u = (A4_W_MM - span_u) / 2
    offset_v = (A4_H_MM - span_v) / 2

    def place(point: Point) -> Point:
        x, y = point
        x_s, y_s = (x - xmin) * scale, (y - ymin) * scale
        u, v = (y_s, span_v - x_s) if rotated else (x_s, y_s)
        out_x = offset_u + u
        if params.mirror:  # left-right flip of the sheet, after rotation
            out_x = A4_W_MM - out_x
        return (out_x, A4_H_MM - (offset_v + v))

    strokes: list[list[Point]] = []
    for layer in SUBLAYER_IDS:
        if layer not in enabled:
            continue
        for shape in shapes:
            if shape.layer != layer:
                continue
            points = [place(point) for point in shape.points]
            strokes.append(points + [points[0]] if shape.closed else points)
    if not strokes:
        raise PenPlotError(
            status=422, code=ErrorCode.INVALID_PARAMS,
            message="None of the enabled sub-layers has anything to plot.",
        )

    pen_down = sum(polyline_length(stroke) for stroke in strokes)
    pen_up = sum(
        dist(strokes[i][-1], strokes[i + 1][0]) for i in range(len(strokes) - 1)
    )
    est = estimate_plot_time_s(pen_down, pen_up, len(strokes), PenParams().pen_lift_s)
    if pen_down > PEN_DOWN_LIMIT_MM or est > PLOT_TIME_LIMIT_S:
        raise PenPlotError(
            status=422, code=ErrorCode.INVALID_PARAMS,
            message=_ceiling_message(pen_down, est),
        )

    groups = sublayer_groups(
        [shape for shape in shapes if shape.layer in enabled],
        place=place,
        inks={layer: INK_HEX[ink] for layer, ink in enabled.items()},
        outline=True,
    )
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" '
        'viewBox="0 0 210 297">\n'
        + "\n".join(groups.values())
        + "\n</svg>\n"
    )
    layers = [
        LayerOut(
            id=layer, ink=enabled[layer],
            paths=sum(1 for shape in shapes if shape.layer == layer),
        )
        for layer in groups
    ]
    stats = SheetStats(
        strokes=len(strokes),
        pen_down_mm=round(pen_down, 2),
        pen_up_mm=round(pen_up, 2),
        estimated_time_s=round(est, 1),
    )
    warnings = [
        f"layer_absent:{layer}"
        for layer in enabled
        if not any(shape.layer == layer for shape in shapes)
    ]
    return SheetResult(svg=svg, stats=stats, layers=layers, rotated=rotated, warnings=warnings)
