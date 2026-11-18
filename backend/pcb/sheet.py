"""Fit a parsed board to one A4 sheet and plot its enabled sub-layers.

Pure geometry: no HTTP, no cache. The board's bounding box covers every
sub-layer of the file (enabled or not), so turning a layer off does not move
the artwork. Closed shapes are drawn as outline strokes (plan D4); tracks are
strokes already. The stroke colour is the resolved pen ink.

Page furniture (``PcbRenderParams.layout`` / ``lines``): the sheet is composed
by ``compose_sheet_svg`` with ``artwork="inline"`` (the ``POST /v1/compose-sheet``
composer) over a stub artwork that fills the free area, so the frame, the band
and the lettering are the composer's own strokes. The board's groups replace
the stub in the composed sheet. Both fields absent: the original A4 portrait
sheet below, unchanged.

Stats come from the same helpers as the convert pipeline: ``polyline_length``
and ``dist`` (``backend.penplot.stages._grid``) and ``estimate_plot_time_s``
(``backend.penplot.pipeline``) with the default pen lift.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass

from backend.fulfillment.ink_split import INK_HEX
from backend.pcb.layers import SUBLAYER_IDS, Point, Shape, sublayer_groups
from backend.pcb.pad_edge import trim_tracks_at_pads
from backend.pcb.render import run_isolated
from backend.pcb.schemas import LayerOut, PcbRenderParams, SheetStats
from backend.penplot import compose_sheet as composer
from backend.penplot.errors import ErrorCode, PenPlotError
from backend.penplot.pipeline import estimate_plot_time_s
from backend.penplot.schemas import (
    ComposeLabelParams,
    ComposeLayout,
    ComposePageParams,
    ComposeSheetRequest,
    PenParams,
)
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


#: The composer's stub artwork group, replaced by the board's groups.
_ART_GROUP = re.compile(r'<g[^>]*data-kp-artwork="1"[^>]*>.*?</g>', re.DOTALL)
_STROKE_POINTS = re.compile(r'<polyline[^>]*?\spoints="([^"]*)"|<path[^>]*?\sd="([^"]*)"')
_PATH_COORDS = re.compile(r"(-?[0-9.]+)\s+(-?[0-9.]+)")

Rect = tuple[float, float, float, float]
#: Size cap handed to the composer; the stub artwork is tiny.
_STUB_MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class _Page:
    """The composed furniture of one page layout."""

    width: float
    height: float
    area: Rect  # free area for the board: x, y, w, h (mm)
    strokes: list[list[Point]]  # furniture strokes in draw order
    embed: Callable[[str], str]  # board groups in, the finished sheet out


def _compose_request(params: PcbRenderParams, svg: str) -> ComposeSheetRequest:
    """The compose-sheet request of ``params`` over the artwork ``svg``."""
    assert params.layout is not None or params.lines
    layout = params.layout
    page = layout.page.model_dump(exclude={"size"}) if layout else {}
    label = layout.label.model_dump(exclude={"enabled", "text", "lines"}) if layout else {}
    return ComposeSheetRequest(
        svg=svg,
        ink_hex=INK_HEX[params.pen],
        layout=ComposeLayout(
            page=ComposePageParams(**page), label=ComposeLabelParams(**label),
        ),
        lines=list(params.lines),
        artwork="inline",
    )


def _furniture_strokes(sheet: str) -> list[list[Point]]:
    """The polylines of the frame, band and lettering of a composed sheet."""
    strokes: list[list[Point]] = []
    for points, path in _STROKE_POINTS.findall(_ART_GROUP.sub("", sheet)):
        if points:
            strokes.append([
                (float(x), float(y))
                for x, y in (pair.split(",") for pair in points.split())
            ])
        else:
            strokes.append([(float(x), float(y)) for x, y in _PATH_COORDS.findall(path)])
    return strokes


def _page(params: PcbRenderParams) -> _Page:
    """Compose the furniture of ``params.layout`` / ``params.lines``.

    A first compose finds the free area (margin, padding, frame inset and the
    title strip, by the composer's own plan); a second one over a stub that
    fills that area gives the sheet with its furniture.
    """
    probe = _compose_request(params, '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>')
    x, y, w, h = composer._plan_sheet(probe, composer._Artwork(None, None, None)).area
    if w <= 0 or h <= 0:
        raise PenPlotError(
            status=422, code=ErrorCode.INVALID_PARAMS,
            message="The page leaves no room for the board.",
        )
    stub = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.4f}" height="{h:.4f}" '
        f'viewBox="0 0 {w:.4f} {h:.4f}"><path d="M0 0 L{w:.4f} {h:.4f}"/></svg>'
    )
    sheet = composer.compose_sheet_svg(
        _compose_request(params, stub), max_svg_bytes=_STUB_MAX_BYTES,
    )
    sheet_w, sheet_h = composer.sheet_size_mm(
        params.layout.page.orientation if params.layout else "portrait"
    )
    if not _ART_GROUP.search(sheet):
        raise PenPlotError(
            status=500, code=ErrorCode.PROCESSING_FAILED,
            message="The page composer did not place the board area.",
        )
    return _Page(
        width=sheet_w, height=sheet_h, area=(x, y, w, h),
        strokes=_furniture_strokes(sheet),
        embed=lambda groups: _ART_GROUP.sub(lambda _m: groups, sheet, count=1),
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
    if width <= 0 and height <= 0:
        raise PenPlotError(
            status=422, code=ErrorCode.INVALID_PARAMS,
            message="The board has no area to plot.",
        )

    page = _page(params) if params.has_page() else None
    if page is None:
        sheet_w, sheet_h = A4_W_MM, A4_H_MM
        box_x, box_y = MARGIN_MM, MARGIN_MM
        avail_w, avail_h = A4_W_MM - 2 * MARGIN_MM, A4_H_MM - 2 * MARGIN_MM
    else:
        sheet_w, sheet_h = page.width, page.height
        box_x, box_y, avail_w, avail_h = page.area

    def fit(span_x: float, span_y: float) -> float:
        # A zero span does not limit that axis (a lone horizontal track).
        limit_x = avail_w / span_x if span_x > 0 else float("inf")
        limit_y = avail_h / span_y if span_y > 0 else float("inf")
        return min(limit_x, limit_y)

    plain_scale = fit(width, height)
    turned_scale = fit(height, width)
    rotated = turned_scale > plain_scale
    scale = turned_scale if rotated else plain_scale
    span_u = (height if rotated else width) * scale
    span_v = (width if rotated else height) * scale
    if page is None:
        offset_u = (sheet_w - span_u) / 2
        offset_v = (sheet_h - span_v) / 2
    else:
        # Centred in the free area; offset_v is the gap below the board.
        offset_u = box_x + (avail_w - span_u) / 2
        offset_v = sheet_h - (box_y + (avail_h + span_v) / 2)

    def place(point: Point) -> Point:
        x, y = point
        x_s, y_s = (x - xmin) * scale, (y - ymin) * scale
        u, v = (y_s, span_v - x_s) if rotated else (x_s, y_s)
        out_x = offset_u + u
        if params.mirror:  # left-right flip of the sheet, after rotation
            out_x = sheet_w - out_x
        return (out_x, sheet_h - (offset_v + v))

    # Tracks stop at the pad outline, but only when the pads are plotted too.
    plotted = trim_tracks_at_pads(shapes) if {"pads", "tracks"} <= enabled.keys() else shapes
    strokes: list[list[Point]] = []
    for layer in SUBLAYER_IDS:
        if layer not in enabled:
            continue
        for shape in plotted:
            if shape.layer != layer:
                continue
            points = [place(point) for point in shape.points]
            strokes.append(points + [points[0]] if shape.closed else points)
    if not strokes:
        raise PenPlotError(
            status=422, code=ErrorCode.INVALID_PARAMS,
            message="None of the enabled sub-layers has anything to plot.",
        )

    if page is not None:  # the frame and the lettering are pen strokes too (D7)
        strokes.extend(page.strokes)

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
        [shape for shape in plotted if shape.layer in enabled],
        place=place,
        inks={layer: INK_HEX[ink] for layer, ink in enabled.items()},
        outline=True,
    )
    if page is None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" '
            'viewBox="0 0 210 297">\n'
            + "\n".join(groups.values())
            + "\n</svg>\n"
        )
    else:
        svg = page.embed("\n".join(groups.values())) + "\n"
    layers = [
        LayerOut(
            id=layer, ink=enabled[layer],
            paths=sum(1 for shape in plotted if shape.layer == layer),
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


async def sheet_from_source(data: bytes, params: PcbRenderParams) -> SheetResult:
    """The A4 sheet of one uploaded Gerber/Excellon file under ``params``.

    The one composition path: ``POST /v1/pcb/render`` and the ink-split
    fallback (re-render from the stored source) both call this.
    """
    shapes = await asyncio.to_thread(run_isolated, "backend.pcb.layers:board_shapes", data)
    return compose_sheet(shapes, params)
