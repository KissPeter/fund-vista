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
drawn: a page frame rect at the margin inset plus one horizontal divider
across the full inner width above the text, joining the left/right frame
verticals and separating the image from the label. Two more pen-down
strokes, same chain as the text.
"""

from __future__ import annotations

from backend.penplot.hershey_fonts import FACES
from backend.penplot.methods import Polyline

WARNING_UNSUPPORTED = "label_unsupported_characters"

DEFAULT_FONT = "futural"
BORDER_PAD_RATIO = 0.3
BORDER_PAD_MIN_MM = 1.0


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
) -> tuple[list[Polyline], list[str]]:
    """Lay out one line of Hershey text in mm space. See module docstring."""
    warnings: list[str] = []
    face = FACES.get(font, FACES[DEFAULT_FONT])
    cap_height = face["cap_height"]
    space_advance = face["space_advance"]
    glyphs = face["glyphs"]
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
        # on the left/right frame verticals.
        pad = max(height_mm * BORDER_PAD_RATIO, BORDER_PAD_MIN_MM)
        gap = min(pad * 0.5, 2.0)
        lines = [[(x, y - gap) for x, y in pl] for pl in lines]
        xs = [x for pl in lines for x, _ in pl]
        ys = [y for pl in lines for _, y in pl]
        divider_y = max(min(ys) - pad, margin_mm)
        frame = [
            (inner_left, margin_mm),
            (inner_right, margin_mm),
            (inner_right, page_h - margin_mm),
            (inner_left, page_h - margin_mm),
            (inner_left, margin_mm),
        ]
        divider = [(inner_left, divider_y), (inner_right, divider_y)]
        lines.append(divider)
        lines.append(frame)
    return lines, warnings
