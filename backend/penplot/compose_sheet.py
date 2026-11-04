"""Gallery sheet compose: POST /v1/compose-sheet (stateless, deterministic).

The shop used to draw the gallery sheet itself (``composeGallerySvg`` in the
shop's ``galleryCompose.ts``). This module moves that composition onto the
server so the plot geometry lives in one place. Layer order matches the shop
mock, bottom of the stack last:

1. the artwork, fitted into the free area with its own aspect ratio. Default
   ``artwork="image"``: embedded byte-for-byte as one ``<image>`` (base64 data
   URI, so nothing in the artwork is re-parsed, re-simplified or re-escaped).
   ``artwork="inline"``: the artwork's own vector markup in one transformed
   ``<g data-kp-artwork>`` at the same box, so the plotter draws it. The group's
   matrix is the image-mode rule (the artwork's viewBox fitted with its own
   preserveAspectRatio). The inline sheet is checked for plottability by
   ``_assert_plottable`` on the polylines it draws, see ``_inline_artwork``;
2. the whole-page margin frame (``labels.page_frame_rect``) when enabled;
3. the title-block band (``labels._band_rect``) when there is a title and the
   border is on, drawn on top of the frame and never replacing it;
4. the lettering, one stroke group per title line (``labels.render_label``);
5. the context table (``stats_table.layout_stats_table``).

Geometry is not re-implemented here: every stroke comes from the functions
named above. The only arithmetic in this module is placement (where the art
box and the title rows sit on the A4 sheet), which mirrors the shop mock.
When a context table is drawn, the artwork is placed clear of the table's real
ink (``art_placement.place_art_around_table``: slide, then shrink), and the
root carries ``data-kp-art-dx/dy/scale/overlap`` describing that placement.
When nothing clears (overlap=1), the root also carries ``data-kp-table-fixes``:
the single-change fixes that do clear it (``suggest_table_fixes``, a port of the
shop's ``suggestTableFixes``). Compose and the suggestions share ``_plan_sheet``.

Validation is the same gate ``POST /v1/images`` applies to an SVG: the size
cap (``max_upload_bytes``, 413), a sniff that the bytes are an SVG document,
and ``imaging.parse_svg_vectors`` (malformed XML, forbidden entities, element
caps, nothing drawable). Every rejection of the input is a 422 envelope.

No input text is logged, only sizes and counts (no PII in logs).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass
from xml.sax.saxutils import escape

from defusedxml import ElementTree as DefusedET
from fastapi import APIRouter, Depends, Response

from backend.penplot import art_placement, labels, stats_table
from backend.penplot.errors import ErrorCode, PenPlotError, sheet_not_plottable
from backend.penplot.methods import Polyline
from backend.penplot.raster import looks_like_svg
from backend.penplot.router import get_settings, require_rate_limit
from backend.penplot.schemas import ComposeContextPads, ComposeSheetRequest
from backend.penplot.svg_vector import Matrix, _parse_viewbox, _viewbox_ctm, parse_svg_vectors

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


def _signed(value: float) -> float:
    """Rounds to the 2 dp the attributes print, folding ``-0.00`` to ``0.00``."""
    return round(value, 2) + 0.0


def _outer_rect(border: list[tuple[float, float]]) -> art_placement.Rect:
    """``(x, y, w, h)`` bounds of the table's outer border polyline."""
    xs = [x for x, _ in border]
    ys = [y for _, y in border]
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _artwork_element(
    data: bytes, *, x: float, y: float, width: float, height: float
) -> str:
    uri = "data:image/svg+xml;base64," + base64.b64encode(data).decode("ascii")
    return (
        f'<image data-kp-artwork="1" href="{uri}" x="{_fmt(x)}" y="{_fmt(y)}" '
        f'width="{_fmt(width)}" height="{_fmt(height)}" />'
    )


#: Elements the inline artwork MUST NOT carry: active or external content, and
#: animation of any kind (SVG 1.1 and SVG Tiny ``handler``/``listener``/``animation``).
#: ``style`` is refused too: parse_svg_vectors ignores CSS, and a selector in
#: the artwork would restyle the frame and lettering the sheet draws.
_UNSAFE_TAGS = frozenset({
    "script", "foreignobject", "style", "iframe", "embed", "object",
    "animate", "animatemotion", "animatetransform", "animatecolor", "set",
    "handler", "listener", "animation",
})
#: Elements the plotted sheet MUST NOT carry: not plottable as polylines.
#: ``marker`` is here because the preview draws markers and the plotter does not.
_UNPLOTTABLE_TAGS = frozenset({"image", "text", "marker"})
#: Attributes that clip or mask the artwork, and the same as CSS properties in a
#: style value. The plotter draws unclipped strokes, so the plot would differ
#: from the preview: refused as artwork_not_plottable.
_CLIP_ATTRS = frozenset({"clip-path", "mask"})
_CLIP_STYLE_RE = re.compile(r"(?:^|;)\s*(?:clip-path|mask[\w-]*)\s*:", re.IGNORECASE)
#: Marker properties (attribute or style). Drawn by the preview, not by the plotter:
#: refused as artwork_not_plottable, for the same reason as clip-path.
_MARKER_ATTRS = frozenset({"marker-start", "marker-mid", "marker-end"})
_MARKER_STYLE_RE = re.compile(r"(?:^|;)\s*marker-(?:start|mid|end)\s*:", re.IGNORECASE)
#: Tolerance in mm for the bounds checks: the sheet prints 2 dp, so an edge
#: stroke may land a hair outside its box or the sheet.
_BOUNDS_TOL_MM = 0.05
#: Prefix on every id of the inline artwork: no id of the artwork can equal a sheet id.
ART_ID_PREFIX = "kp-art-"
#: Root attributes that describe the artwork's own viewport, not its paint.
_ROOT_GEOMETRY_ATTRS = frozenset({
    "x", "y", "width", "height", "viewbox", "preserveaspectratio", "version", "transform",
})
_PROLOG_RE = re.compile(
    r"<\?[\s\S]*?\?>|<!DOCTYPE[^>\[]*(?:\[[\s\S]*?\])?\s*>|<!--[\s\S]*?-->", re.IGNORECASE,
)
_START_TAG_RE = re.compile(r"""<svg\b(?:[^>"']|"[^"]*"|'[^']*')*>""")
_ATTR_RE = re.compile(r"""\s([^\s=/>]+)\s*=\s*("[^"]*"|'[^']*')""")
_END_SVG_RE = re.compile(r"</svg\s*>\s*$")
_URL_RE = re.compile(r"""url\(\s*["']?([^)"'\s]*)""", re.IGNORECASE)
_ID_ATTR_RE = re.compile(r"""(\sid\s*=\s*)(["'])([^"']*)\2""")
#: An href under ANY prefix (``href``, ``xlink:href``, ``xl:href``): the prefix
#: only has to be bound to the xlink namespace, which the id rewrite does not
#: need to know. Every ``url(#...)`` reference is rewritten too.
_ID_REF_RE = re.compile(r"""((?:\s(?:[\w.-]+:)?href)\s*=\s*["']|url\(\s*["']?)#([^"'\s)]+)""")


def _artwork_error(code: str, message: str) -> PenPlotError:
    return PenPlotError(status=422, code=code, message=message)


def _local_name(name: str) -> str:
    """Lower-case local name of a tag or attribute (``{ns}href`` -> ``href``)."""
    return name.split("}")[-1].lower()


def _assert_inline_safe(root) -> None:
    """Refuse the artwork markup an inline sheet must not carry.

    These are this module's own rules. They are not a port of the shop's
    assertSafeSvg and are not claimed to match it. Refused as artwork_unsafe:
    script, foreignObject, CSS ``style``, any animation element, an embedded
    object, an event handler, an external href or url(), a javascript: URL, and
    any backslash in an attribute value (a CSS escape can spell ``url(`` or
    ``javascript:``, so the check runs on the escape-free text). Refused as
    artwork_not_plottable: image, text, a marker element, a nested svg (AxiDraw
    does not descend into one), and clip-path, mask or marker-start/mid/end
    (the plotter would not apply them). Internal
    ``#id`` references are kept.
    """
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        tag = _local_name(el.tag)
        if tag in _UNSAFE_TAGS:
            raise _artwork_error(ErrorCode.ARTWORK_UNSAFE, f"Artwork contains a <{tag}> element.")
        if tag in _UNPLOTTABLE_TAGS or (tag == "svg" and el is not root):
            raise _artwork_error(
                ErrorCode.ARTWORK_NOT_PLOTTABLE,
                f"Artwork contains a <{tag}> element, which cannot be plotted.",
            )
        for name, value in el.attrib.items():
            attr = _local_name(name)
            if "\\" in value:
                raise _artwork_error(
                    ErrorCode.ARTWORK_UNSAFE, "Artwork contains a backslash (CSS escape) in an attribute.",
                )
            if attr.startswith("on"):
                raise _artwork_error(ErrorCode.ARTWORK_UNSAFE, "Artwork contains an event handler.")
            if attr == "href" and not value.strip().startswith("#"):
                raise _artwork_error(ErrorCode.ARTWORK_UNSAFE, "Artwork contains an external href.")
            if "javascript:" in value.lower():
                raise _artwork_error(ErrorCode.ARTWORK_UNSAFE, "Artwork contains a javascript: URL.")
            if any(not target.startswith("#") for target in _URL_RE.findall(value)):
                raise _artwork_error(ErrorCode.ARTWORK_UNSAFE, "Artwork contains an external url().")
            if attr in _CLIP_ATTRS or (attr == "style" and _CLIP_STYLE_RE.search(value)):
                raise _artwork_error(
                    ErrorCode.ARTWORK_NOT_PLOTTABLE,
                    "Artwork clips or masks its content (clip-path or mask), which cannot be plotted.",
                )
            if attr in _MARKER_ATTRS or (attr == "style" and _MARKER_STYLE_RE.search(value)):
                raise _artwork_error(
                    ErrorCode.ARTWORK_NOT_PLOTTABLE,
                    "Artwork uses markers (marker-start, marker-mid or marker-end), which cannot be plotted.",
                )


def _num(value: float) -> str:
    """A matrix term at 6 decimals, trailing zeros dropped (``-0`` folded to ``0``)."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _inline_fit(root, vp_w: float, vp_h: float, box: art_placement.Rect) -> tuple[Matrix, Matrix]:
    """The artwork's viewBox fitted into ``box``, by the artwork's own preserveAspectRatio.

    Returns ``(fit, to_sheet)``. ``fit`` maps the viewBox user space onto the
    sheet in mm. It is the image-mode rule: the root's viewBox into the
    ``<image>`` box with the root's preserveAspectRatio, derived by
    svg_vector._viewbox_ctm (none, x/y Min/Mid/Max, meet, slice). Without a
    viewBox the user space is the artwork's viewport px, as parse_svg_vectors
    reads it. ``to_sheet`` is the affine ``(ax, ay, bx, by)`` that maps the
    viewport px polylines parse_svg_vectors reports straight onto the sheet, so
    the gate reuses the artwork's polylines without parsing it again.
    """
    vb = _parse_viewbox(root.attrib.get("viewBox") or root.attrib.get("viewbox"))
    par = root.attrib.get("preserveAspectRatio")
    vbx, vby, vbw, vbh = vb if vb else (0.0, 0.0, vp_w, vp_h)
    bx, by, bw, bh = box
    fit_ctm = _viewbox_ctm(vbx, vby, vbw, vbh, bw, bh, par)
    fit: Matrix = (fit_ctm[0], 0.0, 0.0, fit_ctm[3], fit_ctm[4] + bx, fit_ctm[5] + by)
    # parse_svg_vectors maps user space to the viewport px with the same rule.
    view_ctm = _viewbox_ctm(vbx, vby, vbw, vbh, vp_w, vp_h, par) if vb else (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    ax = fit[0] / view_ctm[0]
    ay = fit[3] / view_ctm[3]
    to_sheet = (ax, ay, fit[4] - ax * view_ctm[4], fit[5] - ay * view_ctm[5])
    return fit, to_sheet


def _affine_polylines(
    polylines: list[list[tuple[float, float]]], t: tuple[float, float, float, float],
) -> list[list[tuple[float, float]]]:
    """Each point mapped by the diagonal affine ``(ax, ay, bx, by)``."""
    ax, ay, bx, by = t
    return [[(ax * x + bx, ay * y + by) for x, y in pl] for pl in polylines]


def _assert_within_box(polylines: list[list[tuple[float, float]]], box: art_placement.Rect) -> None:
    """Refuse artwork strokes that reach past its box (artwork_not_plottable).

    The preview clips the artwork to its box (the ``<image>`` viewport). The
    plotter cannot clip, so a stroke past the box would plot where the preview
    shows nothing. This happens with a slice that overflows its box.
    """
    x0, y0, bw, bh = box
    for pl in polylines:
        for x, y in pl:
            if not (
                x0 - _BOUNDS_TOL_MM <= x <= x0 + bw + _BOUNDS_TOL_MM
                and y0 - _BOUNDS_TOL_MM <= y <= y0 + bh + _BOUNDS_TOL_MM
            ):
                raise _artwork_error(
                    ErrorCode.ARTWORK_NOT_PLOTTABLE,
                    "Artwork strokes reach past its box; the preview clips them and the plot would not.",
                )


def _prefix_ids(markup: str, ids: set[str]) -> str:
    """Prefix the artwork's own ids and every internal reference to them."""
    if not ids:
        return markup

    def own(m: re.Match[str]) -> str:
        quote, value = m.group(2), m.group(3)
        if value not in ids:
            return m.group(0)
        return f"{m.group(1)}{quote}{ART_ID_PREFIX}{value}{quote}"

    def ref(m: re.Match[str]) -> str:
        if m.group(2) not in ids:
            return m.group(0)
        return f"{m.group(1)}#{ART_ID_PREFIX}{m.group(2)}"

    return _ID_REF_RE.sub(ref, _ID_ATTR_RE.sub(own, markup))


def _inline_artwork(text: str, root, fit: Matrix) -> str:
    """The artwork's own vector markup as ONE transformed ``<g>``.

    Port of the shop's ``inlineArtwork`` in shape: prolog, DOCTYPE and comments
    are dropped; the root's other attributes (paint, style, xml:space, xmlns:*)
    move onto the group; the root's viewBox and preserveAspectRatio are baked
    into ``fit`` (see ``_inline_fit``), so the group sits where the ``<image>``
    form draws the same artwork. ``text`` is the request's SVG str: the request
    bytes are its UTF-8 encoding, so there is no decode step to fail.
    """
    _assert_inline_safe(root)
    body = _PROLOG_RE.sub("", text)
    start = _START_TAG_RE.search(body)
    if start is None:
        raise _artwork_error(ErrorCode.ARTWORK_NOT_PLOTTABLE, "Artwork has no <svg> root.")
    tag = start.group(0)
    kept = "".join(
        f" {name}={value}"
        for name, value in _ATTR_RE.findall(tag)
        if name != "xmlns"
        and not name.startswith("data-kp-")
        and name.lower() not in _ROOT_GEOMETRY_ATTRS
    )
    a, _b, _c, d, e, f = fit
    open_tag = (
        f'<g{kept} data-kp-artwork="1" '
        f'transform="matrix({_num(a)} 0 0 {_num(d)} {_num(e)} {_num(f)})"'
    )
    if tag.endswith("/>"):
        markup = f"{open_tag} />"
    else:
        inner = _END_SVG_RE.sub("", body[start.end():])
        markup = f"{open_tag}>{inner}</g>"
    ids = {el.attrib["id"] for el in root.iter() if "id" in el.attrib}
    return _prefix_ids(markup, ids)


def _assert_plottable(
    sheet: str, plotted: list[list[tuple[float, float]]], w: float, h: float,
) -> None:
    """Plottability gate for an inline sheet, on the polylines the sheet draws.

    ``plotted`` is the artwork's polylines mapped onto the sheet (reused from
    validation, not parsed again) plus the sheet's own furniture polylines.
    Every point MUST lie within the sheet (``_BOUNDS_TOL_MM``), there MUST be
    something to plot, and the sheet MUST carry no <image>, no <text> and one
    root. The input was valid, so a failure is a server fault: 500
    sheet_not_plottable, never a 422 that blames the client.
    """
    if re.search(r"<(?:image|text)[\s>/]", sheet) or len(re.findall(r"<svg[\s>]", sheet)) != 1:
        raise sheet_not_plottable()
    if not any(plotted):
        raise sheet_not_plottable()
    lo = -_BOUNDS_TOL_MM
    hi_x, hi_y = w + _BOUNDS_TOL_MM, h + _BOUNDS_TOL_MM
    for pl in plotted:
        for x, y in pl:
            if not (lo <= x <= hi_x and lo <= y <= hi_y):
                raise sheet_not_plottable()


def _validate_svg(data: bytes, max_bytes: int):
    """Same gate as /v1/images for an SVG.

    Returns ``(root, polylines, width_px, height_px)``: the parsed root element
    and the artwork's viewport-px polylines, kept for the ink mask.
    """
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
        # Same structural and XML-safety checks as the upload path. The
        # polylines are kept for the ink mask; the artwork itself is embedded.
        polylines, width_px, height_px, _warnings = parse_svg_vectors(data)
    except PenPlotError as exc:
        raise PenPlotError(status=422, code=exc.code, message=exc.message) from exc
    return DefusedET.fromstring(data), polylines, width_px, height_px


@dataclass(frozen=True)
class _Artwork:
    """What the layout needs from the artwork: its declared size and its ink mask."""

    root_w: float | None
    root_h: float | None
    mask: art_placement.InkMask | None


@dataclass(frozen=True)
class _Plan:
    """One sheet layout: the geometry compose draws and the table fixes test."""

    w: float
    h: float
    margin: float
    pad: float
    outside: bool
    below: bool
    strip: float
    band_top: float
    table_lines: list[Polyline]
    area: art_placement.Rect
    box: art_placement.Rect
    placed: art_placement.ArtPlacement


def _artwork(root, polylines, vp_w: float, vp_h: float, *, with_mask: bool) -> _Artwork:
    """Artwork facts for the layout. The ink mask is built once per request."""
    mask = art_placement.ink_mask(polylines, vp_w, vp_h) if with_mask else None
    return _Artwork(_root_length(root, "width"), _root_length(root, "height"), mask)


def _plan_sheet(body: ComposeSheetRequest, art: _Artwork) -> _Plan:
    """The sheet layout for one request: area, art box, table strokes and placement.

    Pure and deterministic. Compose draws the plan and the table-fix suggestions
    test candidate requests with this same function, so they cannot drift.
    """
    page, style = body.layout.page, body.layout.label
    w, h = sheet_size_mm(page.orientation)
    margin = page.margin_mm
    pad = page.padding_mm
    lines = [line for line in body.lines if line.strip()]
    title = lines[0] if lines else ""
    subtitle = lines[1] if len(lines) > 1 else ""
    table = body.context_table
    ctx_rows = [(row.label, row.value) for row in table.rows] if table.enabled else []

    # Title block. Inside the border it is one stacked row per line along the
    # bottom edge (the band). Outside (below/above) it is a strip on that side
    # with no band: the artwork and the frame keep clear of the strip.
    line_h = style.height_mm
    outside = style.placement != "inside"
    below = style.placement == "below"
    strip = 0.0
    if outside and lines:
        strip = labels.label_outside_mm(
            title, subtitle=subtitle, height_mm=line_h, font=style.font,
        )
    if outside:
        reserve = strip + labels.LABEL_ARTWORK_GAP_MM if strip > 0.0 else 0.0
        reserve_bottom = reserve if below else 0.0
        reserve_top = 0.0 if below else reserve
    else:
        reserve_bottom = len(lines) * (line_h + LINE_GAP_MM)
        reserve_top = 0.0
    band_top = h - margin - reserve_bottom

    # Context table, laid out before the artwork so the art can avoid it. The
    # drawing itself is emitted by compose, unchanged.
    table_lines: list = []
    if ctx_rows:
        table_lines, _warnings, _keepout = stats_table.layout_stats_table(
            ctx_rows,
            position=table.position,
            page_w=w,
            page_h=h,
            margin_mm=margin,
            reserve_bottom_mm=reserve_bottom,
            reserve_top_mm=reserve_top,
            pad_left_mm=table.pads.left,
            pad_right_mm=table.pads.right,
            pad_top_mm=table.pads.top,
            pad_bottom_mm=table.pads.bottom,
            display_keys=False,
        )

    # Artwork: centred in [margin+pad+top, band_top] x [margin+pad, w-margin-pad],
    # keeping its own aspect when the root declares its size.
    art_top = margin + pad + reserve_top
    area_w = w - 2 * (margin + pad)
    area_h = band_top - art_top
    box_w, box_h = area_w, area_h
    if art.root_w is not None and art.root_h is not None:
        art_aspect = art.root_w / art.root_h
        if art_aspect > area_w / area_h:
            box_h = area_w / art_aspect
        else:
            box_w = area_h * art_aspect
    art_x = (w - box_w) / 2
    art_y = art_top + (area_h - box_h) / 2
    box = (art_x, art_y, box_w, box_h)
    area = (margin + pad, art_top, area_w, area_h)

    # Placement around the table's real ink (shop parity: the table is the
    # outer border box of the drawn table, and the mask comes from the
    # artwork's own polylines). No table: the box is kept as it is.
    placed = art_placement.ArtPlacement(box, 0.0, 0.0, 1.0, False)
    if table_lines:
        placed = art_placement.place_art_around_table(
            area, box, art.mask, _outer_rect(table_lines[0]),
        )
    return _Plan(
        w=w, h=h, margin=margin, pad=pad, outside=outside, below=below,
        strip=strip, band_top=band_top, table_lines=table_lines,
        area=area, box=box, placed=placed,
    )


#: Table corners in the shop's order (``CORNERS`` in the shop's galleryCompose.ts).
CORNERS: tuple[str, ...] = ("top-left", "top-right", "bottom-left", "bottom-right")


def _with_table(body: ComposeSheetRequest, **fields) -> ComposeSheetRequest:
    table = body.context_table.model_copy(update=fields)
    return body.model_copy(update={"context_table": table})


def _with_page(body: ComposeSheetRequest, **fields) -> ComposeSheetRequest:
    page = body.layout.page.model_copy(update=fields)
    return body.model_copy(update={"layout": body.layout.model_copy(update={"page": page})})


def _with_label(body: ComposeSheetRequest, **fields) -> ComposeSheetRequest:
    label = body.layout.label.model_copy(update=fields)
    return body.model_copy(update={"layout": body.layout.model_copy(update={"label": label})})


def suggest_table_fixes(body: ComposeSheetRequest, art: _Artwork) -> list[dict]:
    """Single-change fixes that clear the art/table overlap, cheapest first.

    Port of the shop's ``suggestTableFixes``: same order, kinds and fields. Each
    candidate changes ONE field of the request and is judged by the same
    :func:`_plan_sheet` that compose draws. Empty unless the layout overlaps.
    """
    if not _plan_sheet(body, art).placed.overlap:
        return []

    def clears(candidate: ComposeSheetRequest) -> bool:
        # A candidate clears only if its table still draws. The table is
        # dropped when no remaining cell has a drawable glyph, and then there
        # is no table left to overlap, which must not count as a fix.
        plan = _plan_sheet(candidate, art)
        return bool(plan.table_lines) and not plan.placed.overlap

    current = body.context_table.position
    fixes: list[dict] = []

    # 1. The other three corners.
    for corner in CORNERS:
        if corner != current and clears(_with_table(body, position=corner)):
            fixes.append({"kind": "corner", "corner": corner})

    # 2. All four table pads at 0 (the slider minimum).
    no_pads = ComposeContextPads(left=0, right=0, top=0, bottom=0)
    if clears(_with_table(body, pads=no_pads)):
        fixes.append({"kind": "pads"})

    # 3. Drop the last rows one at a time (n = 1 .. rows-1), the current corner
    # first, then the others; the smallest n with its corner wins.
    rows = body.context_table.rows
    corner_order = [current, *(c for c in CORNERS if c != current)]
    removed = next(
        (
            (n, corner)
            for n in range(1, len(rows))
            for corner in corner_order
            if clears(_with_table(body, position=corner, rows=rows[: len(rows) - n]))
        ),
        None,
    )
    if removed is not None:
        n, corner = removed
        fixes.append({"kind": "removeRows", "n": n, "corner": corner})

    # 4. Flip portrait / landscape.
    flipped = "landscape" if body.layout.page.orientation == "portrait" else "portrait"
    if clears(_with_page(body, orientation=flipped)):
        fixes.append({"kind": "orientation", "orientation": flipped})

    # 5. Page margin and padding at their slider minimums (0 mm).
    if clears(_with_page(body, margin_mm=0.0, padding_mm=0.0)):
        fixes.append({"kind": "margins"})

    # 6. Title block outside the frame: only when it is inside now. The first
    # outside side that clears is the suggestion (the UI applies it as is).
    if body.layout.label.placement == "inside":
        outside = next(
            (p for p in ("below", "above") if clears(_with_label(body, placement=p))),
            None,
        )
        if outside is not None:
            fixes.append({"kind": "labelOutside", "placement": outside})

    # 7. Remove the table altogether.
    fixes.append({"kind": "disableTable"})
    return fixes


def compose_sheet_svg(body: ComposeSheetRequest, *, max_svg_bytes: int) -> str:
    """Compose the A4 gallery sheet SVG. Pure and deterministic.

    Raises :class:`PenPlotError` (422 invalid input, 413 oversize) and never
    returns a partial sheet.
    """
    data = body.svg.encode("utf-8")
    root, polylines, vp_w, vp_h = _validate_svg(data, max_svg_bytes)

    style = body.layout.label
    ink = body.ink_hex
    lines = [line for line in body.lines if line.strip()]
    title = lines[0] if lines else ""
    subtitle = lines[1] if len(lines) > 1 else ""
    table = body.context_table
    ctx_rows = [(row.label, row.value) for row in table.rows] if table.enabled else []

    art = _artwork(root, polylines, vp_w, vp_h, with_mask=bool(ctx_rows))
    plan = _plan_sheet(body, art)
    w, h = plan.w, plan.h
    margin = plan.margin
    outside, below, strip = plan.outside, plan.below, plan.strip
    band_top = plan.band_top
    table_lines = plan.table_lines
    line_h = style.height_mm
    box = plan.placed.box

    art_attrs = ""
    if table_lines:
        placed = plan.placed
        art_attrs = (
            f' data-kp-art-dx="{_signed(placed.dx):.2f}"'
            f' data-kp-art-dy="{_signed(placed.dy):.2f}"'
            f' data-kp-art-scale="{placed.scale:.3f}"'
            f' data-kp-art-overlap="{1 if placed.overlap else 0}"'
        )
        if placed.overlap:
            payload = json.dumps(
                suggest_table_fixes(body, art), separators=(",", ":"),
            )
            art_attrs += (
                f' data-kp-table-fixes="'
                f'{escape(payload, {chr(34): "&quot;", chr(39): "&#39;"})}"'
            )
    parts: list[str] = []
    # The polylines the inline gate checks: the artwork's (mapped onto the sheet)
    # and every furniture polyline emitted below, in the order they are drawn.
    art_sheet: list[list[tuple[float, float]]] = []
    furniture: list[list[tuple[float, float]]] = []
    if body.artwork == "inline":
        fit, to_sheet = _inline_fit(root, vp_w, vp_h, box)
        parts.append(_inline_artwork(body.svg, root, fit))
        art_sheet = _affine_polylines(polylines, to_sheet)
        _assert_within_box(art_sheet, box)
    else:
        parts.append(
            _artwork_element(data, x=box[0], y=box[1], width=box[2], height=box[3])
        )

    # Page furniture: the margin frame (inset by the strip when the title is
    # outside), then the title-block band on top of it (inside only).
    if body.layout.page.frame:
        frame = labels.page_frame_rect(
            w, h, margin, body.layout.page.frame_radius_mm,
            top_mm=margin + strip if style.placement == "above" else None,
            bottom_mm=h - margin - strip if below else None,
        )
        parts.append(_path(frame, ink, 'data-kp-frame="1"'))
        furniture.append(frame)
    border = bool(lines) and style.border and not outside
    if border:
        band = labels._band_rect(
            margin, band_top, w - margin, h - margin, style.border_radius_mm,
        )
        parts.append(_path(band, ink, 'data-kp-title-block="1"'))
        furniture.append(band)

    # Lettering: inside, each line is anchored on its own row by render_label's
    # bottom-anchor, with the row's bottom passed as the virtual page bottom.
    # Outside, the title and subtitle are one stacked block on the margin.
    if outside and lines:
        rows, _warnings = labels.render_label_rows(
            title,
            height_mm=line_h,
            align=style.align,
            page_w=w,
            page_h=h,
            margin_mm=margin,
            placement=style.placement,
            font=style.font,
            pad_left_mm=style.pad_left_mm,
            pad_right_mm=style.pad_right_mm,
            subtitle=subtitle,
        )
        rows_svg: list[str] = []
        for text_, strokes in zip((title, subtitle), rows):
            furniture.extend(strokes)
            rows_svg.append(
                f'<g data-kp-text="{escape(text_, {chr(34): "&quot;"})}">'
                + "".join(_polyline(pl, ink) for pl in strokes)
                + "</g>"
            )
        if rows_svg:
            parts.append(
                f'<g data-kp-label="1" data-kp-label-font="{style.font}" '
                f'data-kp-label-placement="{style.placement}">'
                + "".join(rows_svg)
                + "</g>"
            )
    elif lines:
        rows_svg = []
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
            furniture.extend(strokes)
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
        if table_lines:
            # Layout order: outer border, column divider, one rule per row
            # boundary, then the cell strokes.
            grid_count = 2 + (len(ctx_rows) - 1)
            grid, text = table_lines[:grid_count], table_lines[grid_count:]
            outer = _path(grid[0], ink, 'data-kp-context-border="1"')
            rules = "".join(_polyline(pl, ink, ' data-kp-context-rule="1"') for pl in grid[1:])
            furniture.extend(grid)
            # Cell lettering, one marked group per non-blank cell in layout
            # order (key then value, row by row), so the table stays
            # assertable: the stroke count per cell is the one
            # layout_stats_table itself drew (_text_lines of the same text).
            cells = [text_ for row in ctx_rows for text_ in (row[0], str(row[1]))]
            groups: list[str] = []
            at = 0
            for cell in cells:
                n = len(stats_table._text_lines(cell, stats_table.HEIGHT_MM)[0])
                if n == 0:
                    continue
                strokes_svg = "".join(_polyline(pl, ink) for pl in text[at : at + n])
                furniture.extend(text[at : at + n])
                at += n
                groups.append(f'<g data-kp-text="{escape(cell, {chr(34): "&quot;"})}">{strokes_svg}</g>')
            text_svg = "".join(groups)
            parts.append(
                f'<g data-kp-context="1" data-kp-context-position="{table.position}">'
                f"{outer}{rules}"
                f'<g data-kp-context-text="1">{text_svg}</g></g>'
            )

    width_mm = f"{w:g}"
    height_mm = f"{h:g}"
    # Outside placements carry the sheet bounds on the root, not in a rect:
    # vpype plots an invisible rect (fill and stroke none) as a path.
    bounds_attr = f' data-kp-a4-bounds="0 0 {width_mm} {height_mm}"' if outside else ""
    sheet = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_mm}mm" '
        f'height="{height_mm}mm" viewBox="0 0 {width_mm} {height_mm}" '
        f'data-kp-compose="1"{bounds_attr}{art_attrs}>'
        + "".join(parts)
        + "</svg>"
    )
    if body.artwork == "inline":
        _assert_plottable(sheet, art_sheet + furniture, w, h)
    log.info(
        "compose.sheet svg_bytes=%d lines=%d context_rows=%d frame=%s border=%s",
        len(data), len(lines), len(ctx_rows), body.layout.page.frame, border,
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
