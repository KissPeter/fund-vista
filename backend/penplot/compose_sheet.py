"""Gallery sheet compose: POST /v1/compose-sheet (stateless, deterministic).

The shop used to draw the gallery sheet itself (``composeGallerySvg`` in the
shop's ``galleryCompose.ts``). This module moves that composition onto the
server so the plot geometry lives in one place. Layer order matches the shop
mock, bottom of the stack last:

1. the artwork, embedded byte-for-byte as one ``<image>`` (base64 data URI,
   so nothing in the artwork is re-parsed, re-simplified or re-escaped), fitted
   into the free area with its own aspect ratio;
2. the whole-page margin frame (``labels.page_frame_rect``) when enabled;
3. the title-block band (``labels._band_rect``) when there is a title and the
   border is on, drawn on top of the frame and never replacing it;
4. the lettering, one stroke group per title line (``labels.render_label``);
5. the context table (``stats_table.layout_stats_table``).

Geometry is not re-implemented here: every stroke comes from the functions
named above. The only arithmetic in this module is placement (where the art
box and the title rows sit on the A4 sheet), which mirrors the shop mock.

Validation is the same gate ``POST /v1/images`` applies to an SVG: the size
cap (``max_upload_bytes``, 413), a sniff that the bytes are an SVG document,
and ``imaging.parse_svg_vectors`` (malformed XML, forbidden entities, element
caps, nothing drawable). Every rejection of the input is a 422 envelope.

No input text is logged, only sizes and counts (no PII in logs).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from xml.sax.saxutils import escape

from defusedxml import ElementTree as DefusedET
from fastapi import APIRouter, Depends, Response

from backend.penplot import labels, stats_table
from backend.penplot.errors import ErrorCode, PenPlotError
from backend.penplot.raster import looks_like_svg
from backend.penplot.router import get_settings, require_rate_limit
from backend.penplot.schemas import ComposeSheetRequest
from backend.penplot.svg_vector import parse_svg_vectors

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["penplot-v1"])

#: Gap between stacked title-block lines, in mm (shop mock ``LINE_GAP_MM``).
LINE_GAP_MM = 1.5
#: Pen stroke width used for every drawn element, in mm (shop mock).
STROKE_MM = 0.4

_NUMBER_RE = re.compile(r"\s*([0-9]+(?:\.[0-9]*)?|\.[0-9]+)")


def sheet_size_mm(orientation: str) -> tuple[float, float]:
    """A4 sheet in mm: 210x297 portrait, 297x210 landscape."""
    return (297.0, 210.0) if orientation == "landscape" else (210.0, 297.0)


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def _polyline(points: list[tuple[float, float]], ink: str, marker: str = "") -> str:
    pts = " ".join(f"{_fmt(x)},{_fmt(y)}" for x, y in points)
    return (
        f'<polyline data-kp-stroke="1"{marker} points="{pts}" fill="none" '
        f'stroke="{ink}" stroke-width="{STROKE_MM}" stroke-linecap="round" '
        f'stroke-linejoin="round" />'
    )


def _path(points: list[tuple[float, float]], ink: str, marker: str) -> str:
    """Closed or open polyline as one SVG path (frame / band)."""
    d = "M " + " L ".join(f"{_fmt(x)} {_fmt(y)}" for x, y in points)
    return f'<path {marker} d="{d}" fill="none" stroke="{ink}" stroke-width="{STROKE_MM}" />'


def _root_length(root, attr: str) -> float | None:
    """Positive leading number of a root length attribute (``210mm`` -> 210)."""
    raw = root.attrib.get(attr)
    if not raw:
        return None
    m = _NUMBER_RE.match(raw)
    if not m:
        return None
    value = float(m.group(1))
    return value if value > 0 else None


def _artwork_element(
    data: bytes, *, x: float, y: float, width: float, height: float
) -> str:
    uri = "data:image/svg+xml;base64," + base64.b64encode(data).decode("ascii")
    return (
        f'<image data-kp-artwork="1" href="{uri}" x="{_fmt(x)}" y="{_fmt(y)}" '
        f'width="{_fmt(width)}" height="{_fmt(height)}" />'
    )


def _validate_svg(data: bytes, max_bytes: int):
    """Same gate as /v1/images for an SVG; returns the parsed root element."""
    if len(data) > max_bytes:
        raise PenPlotError(
            status=413, code=ErrorCode.PAYLOAD_TOO_LARGE,
            message=f"SVG exceeds {max_bytes // (1024 * 1024)} MB limit.",
        )
    if not looks_like_svg(data):
        raise PenPlotError(
            status=422, code=ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            message="Artwork must be an SVG document.",
        )
    try:
        # Same structural and XML-safety checks as the upload path; the
        # extracted polylines are discarded because the artwork is embedded.
        parse_svg_vectors(data)
    except PenPlotError as exc:
        raise PenPlotError(status=422, code=exc.code, message=exc.message) from exc
    return DefusedET.fromstring(data)


def compose_sheet_svg(body: ComposeSheetRequest, *, max_svg_bytes: int) -> str:
    """Compose the A4 gallery sheet SVG. Pure and deterministic.

    Raises :class:`PenPlotError` (422 invalid input, 413 oversize) and never
    returns a partial sheet.
    """
    data = body.svg.encode("utf-8")
    root = _validate_svg(data, max_svg_bytes)

    page, style = body.layout.page, body.layout.label
    ink = body.ink_hex
    w, h = sheet_size_mm(page.orientation)
    margin = page.margin_mm
    pad = page.padding_mm
    lines = [line for line in body.lines if line.strip()]
    table = body.context_table
    ctx_rows = [(row.label, row.value) for row in table.rows] if table.enabled else []

    # Title block: one stacked row per line, along the bottom edge.
    line_h = style.height_mm
    label_band = len(lines) * (line_h + LINE_GAP_MM)
    band_top = h - margin - label_band

    # Artwork: centred in [margin+pad, band_top] x [margin+pad, w-margin-pad],
    # keeping its own aspect when the root declares its size.
    art_bottom = band_top
    area_w = w - 2 * (margin + pad)
    area_h = art_bottom - margin - pad
    box_w, box_h = area_w, area_h
    raw_w = _root_length(root, "width")
    raw_h = _root_length(root, "height")
    if raw_w is not None and raw_h is not None:
        art_aspect = raw_w / raw_h
        if art_aspect > area_w / area_h:
            box_h = area_w / art_aspect
        else:
            box_w = area_h * art_aspect
    art_x = (w - box_w) / 2
    art_y = (art_bottom + margin + pad - box_h) / 2
    parts: list[str] = [
        _artwork_element(data, x=art_x, y=art_y, width=box_w, height=box_h)
    ]

    # Page furniture: the margin frame, then the title-block band on top of it.
    if page.frame:
        frame = labels.page_frame_rect(w, h, margin, page.frame_radius_mm)
        parts.append(_path(frame, ink, 'data-kp-frame="1"'))
    border = bool(lines) and style.border
    if border:
        band = labels._band_rect(
            margin, band_top, w - margin, h - margin, style.border_radius_mm,
        )
        parts.append(_path(band, ink, 'data-kp-title-block="1"'))

    # Lettering: each line is anchored on its own row by render_label's
    # bottom-anchor, with the row's bottom passed as the virtual page bottom.
    if lines:
        rows_svg: list[str] = []
        for i, line in enumerate(lines):
            row_bottom = band_top + i * (line_h + LINE_GAP_MM) + line_h + LINE_GAP_MM / 2
            strokes, _warnings = labels.render_label(
                line,
                height_mm=line_h,
                align=style.align,
                page_w=w,
                page_h=row_bottom + margin,
                margin_mm=margin,
                font=style.font,
                border=False,
                pad_left_mm=style.pad_left_mm,
                pad_right_mm=style.pad_right_mm,
            )
            strokes_svg = "".join(_polyline(pl, ink) for pl in strokes)
            text_attr = escape(line, {'"': "&quot;"})
            rows_svg.append(f'<g data-kp-text="{text_attr}">{strokes_svg}</g>')
        parts.append(
            f'<g data-kp-label="1" data-kp-label-font="{style.font}">'
            + "".join(rows_svg)
            + "</g>"
        )

    # Context table: rule and border strokes first, then the cell lettering.
    if ctx_rows:
        table_lines, _warnings, _keepout = stats_table.layout_stats_table(
            ctx_rows,
            position=table.position,
            page_w=w,
            page_h=h,
            margin_mm=margin,
            reserve_bottom_mm=label_band,
            pad_left_mm=table.pads.left,
            pad_right_mm=table.pads.right,
            pad_top_mm=table.pads.top,
            pad_bottom_mm=table.pads.bottom,
            display_keys=False,
        )
        if table_lines:
            # Layout order: outer border, column divider, one rule per row
            # boundary, then the cell strokes.
            grid_count = 2 + (len(ctx_rows) - 1)
            grid, text = table_lines[:grid_count], table_lines[grid_count:]
            outer = _path(grid[0], ink, 'data-kp-context-border="1"')
            rules = "".join(_polyline(pl, ink, ' data-kp-context-rule="1"') for pl in grid[1:])
            text_svg = "".join(_polyline(pl, ink) for pl in text)
            parts.append(
                f'<g data-kp-context="1" data-kp-context-position="{table.position}">'
                f"{outer}{rules}"
                f'<g data-kp-context-text="1">{text_svg}</g></g>'
            )

    width_mm = f"{w:g}"
    height_mm = f"{h:g}"
    sheet = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_mm}mm" '
        f'height="{height_mm}mm" viewBox="0 0 {width_mm} {height_mm}" '
        f'data-kp-compose="1">'
        + "".join(parts)
        + "</svg>"
    )
    log.info(
        "compose.sheet svg_bytes=%d lines=%d context_rows=%d frame=%s border=%s",
        len(data), len(lines), len(ctx_rows), page.frame, border,
    )
    return sheet


@router.post(
    "/compose-sheet",
    response_class=Response,
    responses={200: {"content": {"image/svg+xml": {}}, "description": "Composed A4 sheet."}},
    dependencies=[Depends(require_rate_limit)],
)
async def compose_sheet_endpoint(body: ComposeSheetRequest) -> Response:
    """Compose the gallery sheet: artwork + page furniture + title + context table."""
    svg = await asyncio.to_thread(
        compose_sheet_svg, body, max_svg_bytes=get_settings().max_upload_bytes,
    )
    return Response(content=svg, media_type="image/svg+xml")
