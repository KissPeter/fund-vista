"""Blueprint-style title labels in single-stroke Hershey (Drawscape-style).

A label is one line of plotter text set directly in page-millimetre space and
appended to the laid-out geometry *before* quantize, so it snaps to the same
0.02 mm grid, joins travel sorting and is counted in stats like any other
stroke. Single-stroke (centerline) glyphs from ``hershey_fonts`` — never
outlined/filled shapes, so the pen draws each stem exactly once.

Faces mirror Drawscape's hershey-text select (``futural`` default, ``futuram``,
``simplex``). futural/futuram cover ASCII 33-126 incl. lowercase; simplex is
the compact caps/digits/punct face.

Placement: bottom strip just inside the page margins (classic title-block
corner). Horizontal alignment within the inner width:

- ``left`` — line starts at the left margin;
- ``right`` — line ends at the right margin (default);
- ``fill`` — glyphs are stretched horizontally to span the full inner width.

A line that is naturally wider than the inner width is scaled down to fit
(``left``/``right``); an empty/blank text is a silent no-op. Characters
outside the face set are skipped and reported once via
``label_unsupported_characters``.

Border: when ``border`` is true (default) a blueprint title-block strip is
drawn: a page frame rect at the margin inset (corner radius
``border_radius_mm``, 0 is sharp) plus one horizontal divider
across the full inner width above the text, joining the left/right frame
verticals and separating the image from the label. Two more pen-down
strokes, same chain as the text.

Outline faces (``excalifont``, ``comic-shanns``, ``nunito``) are TTF outlines
traced to polylines — the pen draws every stem twice (once per outline side),
unlike the single-stroke Hershey faces. They flow through the same layout,
strip, border and cleanup code; only the glyph source differs. Glyphs that
rasterize empty are skipped with the same ``label_unsupported_characters``
warning (note: a TTF ``.notdef`` tofu box is non-empty, so truly-missing
glyphs in these large-coverage faces plot as tofu rather than warning —
same as any text renderer).
"""

from __future__ import annotations

import os

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from backend.penplot.hershey_fonts import FACES
from backend.penplot.methods import Polyline

WARNING_UNSUPPORTED = "label_unsupported_characters"

DEFAULT_FONT = "futural"
#: Select order for the UI font picker (Hershey single-stroke first).
LABEL_FONTS = (
    "futural", "futuram", "simplex",
    "excalifont", "comic-shanns", "nunito",
)
#: Hybrid outline faces: TTF file per label font name (OFL/MIT, see below).
_OUTLINE_FILES = {
    "excalifont": "Excalifont-Regular.ttf",
    "comic-shanns": "ComicShanns-Regular.ttf",
    "nunito": "Nunito-Regular.ttf",
}
#: Raster em size for outline tracing; contours scale down to mm on layout.
OUTLINE_RENDER_PX = 200

ATTRIBUTION_OUTLINE = (
    "Excalifont (c) Excalidraw / Jan Filipek (DizajnDesign), SIL OFL 1.1; "
    "Comic Shanns (c) Shannon Miwa, MIT; "
    "Nunito (c) Vernon Adams, SIL OFL 1.1 (weight-500 Latin subset, "
    "Excalidraw build). TTFs vendored under backend/penplot/fonts/."
)

_outline_cache: dict[str, dict] = {}
DEFAULT_BORDER_RADIUS_MM = 2.0
BORDER_PAD_RATIO = 0.3
BORDER_PAD_MIN_MM = 1.0
CORNER_SEGMENTS = 8
# Breathing room between artwork and the strip so the cleanup chain
# (linemerge, default 0.5 mm) can't fuse image strokes into divider/frame.
LABEL_ARTWORK_GAP_MM = 1.0


def _outline_face(name: str) -> dict:
    """Cached TTF entry: PIL font, ascent/descent, cap height, space advance."""
    entry = _outline_cache.get(name)
    if entry is None:
        path = os.path.join(
            os.path.dirname(__file__), "fonts", _OUTLINE_FILES[name])
        font = ImageFont.truetype(path, OUTLINE_RENDER_PX)
        try:
            ascent, descent = font.getmetrics()
        except Exception:
            ascent, descent = int(OUTLINE_RENDER_PX * 0.8), int(OUTLINE_RENDER_PX * 0.2)
        entry = {
            "font": font, "ascent": ascent, "descent": descent,
            "cap": _outline_cap_px(font, ascent, descent),
            "space": font.getlength(" "),
        }
        _outline_cache[name] = entry
    return entry


def _trace_mask(mask: np.ndarray) -> list[list[tuple[float, float]]]:
    """Binary-white-on-black mask -> contour polylines (imaging.py convention)."""
    _, bw = cv2.threshold(mask, 127, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(bw, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    out: list[list[tuple[float, float]]] = []
    for cnt in contours:
        if cv2.contourArea(cnt) < 2.0:
            continue
        seq = cnt.reshape(-1, 2)
        if len(seq) < 2:
            continue
        out.append([(float(px), float(py)) for px, py in seq])
    return out


def _outline_cap_px(font, ascent: int, descent: int) -> float:
    """Cap height in render px, measured off the H contours (fallback: ascent)."""
    pad = 8
    canvas_w = int(font.getlength("H")) + 2 * pad
    canvas_h = ascent + descent + 2 * pad
    img = Image.new("L", (max(canvas_w, 1), max(canvas_h, 1)), 0)
    ImageDraw.Draw(img).text((pad, pad + ascent), "H", font=font,
                             fill=255, anchor="ls")
    lines = _trace_mask(np.asarray(img, dtype=np.uint8))
    if not lines:
        return float(ascent)
    ys = [y for line in lines for _, y in line]
    return max(ys) - min(ys)


def _outline_glyph(entry: dict, ch: str) -> dict | None:
    """One TTF glyph -> {"advance", "lines"} in render px, baseline y=0, y-down.

    Returns None when the glyph rasterizes empty (caller warns, like Hershey).
    """
    font = entry["font"]
    ascent, descent = entry["ascent"], entry["descent"]
    adv = font.getlength(ch)
    pad = 8
    canvas_w = max(1, int(adv) + 2 * pad)
    canvas_h = ascent + descent + 2 * pad
    img = Image.new("L", (canvas_w, canvas_h), 0)
    ImageDraw.Draw(img).text((pad, pad + ascent), ch, font=font,
                             fill=255, anchor="ls")
    lines = _trace_mask(np.asarray(img, dtype=np.uint8))
    if not lines:
        return None
    ox, oy = float(pad), float(pad + ascent)
    return {
        "advance": float(adv),
        "lines": [[(x - ox, y - oy) for x, y in line] for line in lines],
    }


def _resolve_face(
    font: str, chars: list[str]
) -> tuple[float, float, dict]:
    """(cap_height, space_advance, drawables-only glyphs) in face units.

    Hershey faces come from the vendored table; outline faces rasterize the
    needed glyphs on demand (cached per process). Missing glyphs are absent
    from the returned dict so callers warn uniformly.
    """
    if font not in FACES and font not in _OUTLINE_FILES:
        font = DEFAULT_FONT
    if font in FACES:
        face = FACES[font]
        return face["cap_height"], face["space_advance"], face["glyphs"]
    entry = _outline_face(font)
    glyphs: dict[str, dict] = {}
    for ch in chars:
        if ch == " " or ch in glyphs:
            continue
        g = _outline_glyph(entry, ch)
        if g is not None:
            glyphs[ch] = g
    return entry["cap"], entry["space"], glyphs


def _strip_metrics(
    text: str, *, height_mm: float, font: str
) -> tuple[float, float, float] | None:
    """(text height, pad, gap) in mm for drawable glyphs; None if nothing draws."""
    cap_height, _, glyphs = _resolve_face(font, list(text))
    s = max(height_mm, 1e-9) / cap_height
    ys: list[float] = []
    for ch in text:
        if ch == " ":
            continue
        glyph = glyphs.get(ch)
        if glyph is None:
            continue
        ys.extend(y * s for _, y in (pt for stroke in glyph["lines"] for pt in stroke))
    if not ys:
        return None
    text_h = max(ys) - min(ys)
    pad = max(height_mm * BORDER_PAD_RATIO, BORDER_PAD_MIN_MM)
    gap = min(pad * 0.5, 2.0)
    return text_h, pad, gap


def label_reserve_mm(
    text: str, *, height_mm: float, font: str = DEFAULT_FONT, border: bool = True
) -> float:
    """Vertical mm the title strip occupies above the bottom margin.

    The pipeline reserves this from the image area so artwork bottoms out
    exactly on the divider (border) or the text top (borderless). Blank or
    fully-unsupported text reserves nothing — matching render_label's no-op.
    """
    m = _strip_metrics(text, height_mm=height_mm, font=font)
    if m is None:
        return 0.0
    text_h, pad, gap = m
    return text_h + (pad + gap if border else 0.0)


def _rounded_rect(    x0: float, y0: float, x1: float, y1: float, r: float
) -> Polyline:
    """Closed rounded-rectangle polyline (arcs as short chords); r=0 is sharp."""
    r = max(0.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    if r <= 1e-9:
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    import math

    pts: Polyline = [(x0 + r, y0), (x1 - r, y0)]
    for cx, cy, a0 in (
        (x1 - r, y0 + r, -90.0),
        (x1 - r, y1 - r, 0.0),
        (x0 + r, y1 - r, 90.0),
        (x0 + r, y0 + r, 180.0),
    ):
        for i in range(1, CORNER_SEGMENTS + 1):
            a = math.radians(a0 + 90.0 * i / CORNER_SEGMENTS)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    pts.append(pts[0])
    return pts


def page_frame_rect(
    page_w: float, page_h: float, margin_mm: float, radius_mm: float
) -> Polyline:
    """Whole-page margin frame (rounded), independent of any label."""
    return _rounded_rect(
        margin_mm, margin_mm, page_w - margin_mm, page_h - margin_mm, radius_mm
    )


def render_label(
    text: str,
    *,
    height_mm: float,
    align: str,
    page_w: float,
    page_h: float,
    margin_mm: float,
    font: str = DEFAULT_FONT,
    border: bool = True,
    pad_left_mm: float = 0.0,
    pad_right_mm: float = 0.0,
    border_radius_mm: float = DEFAULT_BORDER_RADIUS_MM,
) -> tuple[list[Polyline], list[str]]:
    """Lay out one line of label text in mm space. See module docstring."""
    warnings: list[str] = []
    cap_height, space_advance, glyphs = _resolve_face(font, list(text))
    s = max(height_mm, 1e-9) / cap_height
    lines: list[Polyline] = []
    missing: list[str] = []
    cursor = 0.0
    for ch in text:
        if ch == " ":
            cursor += space_advance * s
            continue
        glyph = glyphs.get(ch)
        if glyph is None:
            if ch not in missing:
                missing.append(ch)
            continue
        for stroke in glyph["lines"]:
            lines.append([(cursor + x * s, y * s) for x, y in stroke])
        cursor += glyph["advance"] * s
    if missing:
        shown = "".join(sorted(missing))[:20]
        warnings.append(f"{WARNING_UNSUPPORTED}:{shown}")
    if not lines:
        return [], warnings

    line_w = cursor
    inner_left = margin_mm
    inner_right = page_w - margin_mm
    inner_w = max(inner_right - inner_left, 1e-9)
    # Text insets from the frame verticals; the frame/divider still span the
    # full inner width. Oversized pads collapse to a tiny centered slot so an
    # over-wide line scales down instead of inverting.
    text_left = inner_left + max(pad_left_mm, 0.0)
    text_right = inner_right - max(pad_right_mm, 0.0)
    if text_left >= text_right:
        mid = (inner_left + inner_right) / 2.0
        text_left, text_right = mid - 0.5, mid + 0.5
    text_w = max(text_right - text_left, 1e-9)

    def _bbox() -> tuple[float, float]:
        xs = [x for pl in lines for x, _ in pl]
        return min(xs), max(xs)

    min_x, max_x = _bbox()
    actual_w = max_x - min_x
    if actual_w > text_w:
        # Too wide for the padded slot: scale the whole line down to fit.
        fit = text_w / actual_w
        lines = [[(x * fit, y) for x, y in pl] for pl in lines]
        min_x, max_x = _bbox()
        actual_w = max_x - min_x
    if align == "fill" and actual_w > 1e-9:
        # Non-uniform x-stretch to span the padded slot.
        sx = text_w / actual_w
        lines = [[(text_left + (x - min_x) * sx, y) for x, y in pl] for pl in lines]
    elif align == "left":
        dx = text_left - min_x
        if dx != 0.0:
            lines = [[(x + dx, y) for x, y in pl] for pl in lines]
    else:  # right (default): glyph bbox ends exactly at the padded edge
        dx = text_right - max_x
        if dx != 0.0:
            lines = [[(x + dx, y) for x, y in pl] for pl in lines]

    # Anchor the line's bbox bottom just inside the bottom margin.
    ys = [y for pl in lines for _, y in pl]
    lines = [[(x, y + (page_h - margin_mm - max(ys))) for x, y in pl] for pl in lines]

    if border:
        # Blueprint title-block strip: lift the text off the frame bottom,
        # then span a divider across the full inner width so it lands exactly
        # on the left/right frame verticals. pad/gap come from _strip_metrics
        # so label_reserve_mm (image-area reservation) always agrees.
        m = _strip_metrics(text, height_mm=height_mm, font=font)
        assert m is not None  # lines non-empty implies drawable glyphs
        _, pad, gap = m
        lines = [[(x, y - gap) for x, y in pl] for pl in lines]
        xs = [x for pl in lines for x, _ in pl]
        ys = [y for pl in lines for _, y in pl]
        divider_y = max(min(ys) - pad, margin_mm)
        frame = _rounded_rect(
            inner_left, margin_mm, inner_right, page_h - margin_mm,
            border_radius_mm,
        )
        divider = [(inner_left, divider_y), (inner_right, divider_y)]
        lines.append(divider)
        lines.append(frame)
    return lines, warnings
