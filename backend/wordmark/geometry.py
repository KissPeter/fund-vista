"""Negative-space wordmark geometry: glyph outlines + dot cuts.

Faithful port of ``negspace_logo.py`` (fontTools outlines + skia-pathops
booleans), adapted from a CLI poster script to a service module:

* the font resolves to a vendored OFL file (no ``/usr/share`` dependency),
* curves are flattened once, up front, so every contour downstream is a
  polygon (stable boolean inputs, compact SVG output),
* ``render_wordmark`` is the single entry point returning SVG + counts,
* failures raise :class:`PenPlotError` (422 ``invalid_params``) instead of
  ``argparse`` errors.

Deliberately deferred to a later version (the reference CLI has them, the
product API does not need them yet): per-letter ``--erase/--keep/--add``
sculpting. ``solid`` / ``knockout`` / ``layered`` modes, tracking, gap and
explicit cuts are all supported.
"""

from __future__ import annotations

import hashlib
import math
import os

import pathops
from fontTools.pens.basePen import BasePen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

from backend.penplot import svgmeta
from backend.penplot.errors import invalid_params
from backend.wordmark.schemas import FONTS, CutSpec

_INK = "#111111"


def font_path(font: str) -> str:
    """Absolute path of a vendored face (``font`` is schema-validated)."""
    try:
        filename = FONTS[font]
    except KeyError:
        raise invalid_params(f"Unknown wordmark font '{font}'.") from None
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", filename)


_fonts: dict[str, TTFont] = {}


def load_font(font: str) -> TTFont:
    """Process-cached font (read-only use is thread-safe)."""
    cached = _fonts.get(font)
    if cached is None:
        try:
            cached = TTFont(font_path(font))
        except OSError as exc:
            raise invalid_params(f"Wordmark font '{font}' is unavailable.") from exc
        _fonts[font] = cached
    return cached


def geometry_source_version() -> str:
    """Content hash of this file, baked into the SVG cache key.

    Mirrors the airports ``render_source_version`` pattern: any geometry
    change automatically retires stale cached renders.
    """
    with open(os.path.abspath(__file__), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:12]


class FlattenPen(BasePen):
    """Draw a glyph into contour polygons, translated by (dx, 0).

    Curves are subdivided to lines on the way in (tolerance scales with the
    em size), so every contour leaving this pen is a closed polygon —
    deterministic input for the boolean stage.
    """

    def __init__(self, glyphset, contours: list[list[tuple[float, float]]],
                 tol: float, dx: float = 0.0):
        super().__init__(glyphset)
        self._contours, self._tol, self._dx = contours, tol, dx
        self._current: list[tuple[float, float]] | None = None
        self._start: tuple[float, float] | None = None

    def _moveTo(self, pt):
        self._current = [(pt[0] + self._dx, pt[1])]
        self._start = self._current[0]

    def _lineTo(self, pt):
        assert self._current is not None
        self._current.append((pt[0] + self._dx, pt[1]))

    def _curveToOne(self, a, b, c):
        assert self._current is not None
        p0 = self._current[-1]
        p1 = (a[0] + self._dx, a[1])
        p2 = (b[0] + self._dx, b[1])
        p3 = (c[0] + self._dx, c[1])
        self._current.extend(_flatten_cubic(p0, p1, p2, p3, self._tol))

    def _qCurveToOne(self, a, b):
        assert self._current is not None
        p0 = self._current[-1]
        p1 = (a[0] + self._dx, a[1])
        p2 = (b[0] + self._dx, b[1])
        self._current.extend(_flatten_quad(p0, p1, p2, self._tol))

    def _closePath(self):
        assert self._current is not None
        if len(self._current) >= 3:
            self._contours.append(self._current)
        self._current, self._start = None, None

    def _endPath(self):
        if self._current and len(self._current) >= 3:
            self._contours.append(self._current)
        self._current, self._start = None, None


def _flatten_cubic(p0, p1, p2, p3, tol: float) -> list[tuple[float, float]]:
    mx1 = ((p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0)
    mx2 = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
    mx3 = ((p2[0] + p3[0]) / 2.0, (p2[1] + p3[1]) / 2.0)
    mx12 = ((mx1[0] + mx2[0]) / 2.0, (mx1[1] + mx2[1]) / 2.0)
    mx23 = ((mx2[0] + mx3[0]) / 2.0, (mx2[1] + mx3[1]) / 2.0)
    cx, cy = (mx12[0] + mx23[0]) / 2.0, (mx12[1] + mx23[1]) / 2.0
    lx, ly = p3[0] - p0[0], p3[1] - p0[1]
    dist = abs(lx * (p0[1] - cy) - ly * (p0[0] - cx)) / max(math.hypot(lx, ly), 1e-9)
    if dist <= tol:
        return [p3]
    left = _flatten_cubic(p0, mx1, mx12, (cx, cy), tol)
    right = _flatten_cubic((cx, cy), mx23, mx3, p3, tol)
    return left + right


def _flatten_quad(p0, p1, p2, tol: float) -> list[tuple[float, float]]:
    cx = (p0[0] + 2.0 * p1[0] + p2[0]) / 4.0
    cy = (p0[1] + 2.0 * p1[1] + p2[1]) / 4.0
    lx, ly = p2[0] - p0[0], p2[1] - p0[1]
    dist = abs(lx * (p0[1] - cy) - ly * (p0[0] - cx)) / max(math.hypot(lx, ly), 1e-9)
    if dist <= tol:
        return [p2]
    mx1 = ((p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0)
    mx2 = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
    mid = ((mx1[0] + mx2[0]) / 2.0, (mx1[1] + mx2[1]) / 2.0)
    return _flatten_quad(p0, mx1, mid, tol) + _flatten_quad(mid, mx2, p2, tol)


def circle(cx: float, cy: float, r: float, n: int = 64) -> pathops.Path:
    p = pathops.Path()
    pen = p.getPen()
    pts = [(cx + r * math.cos(2 * math.pi * i / n),
            cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]
    pen.moveTo(pts[0])
    for pt in pts[1:]:
        pen.lineTo(pt)
    pen.closePath()
    return p


def _draw_letter(font: TTFont, char: str, dx: float, tol: float) -> pathops.Path:
    """One glyph as a pathops path (flattened polygons, translated by dx)."""
    glyphset = font.getGlyphSet()
    path = pathops.Path()
    pen = path.getPen(glyphSet=glyphset)
    contours: list[list[tuple[float, float]]] = []
    glyphset[font.getBestCmap()[ord(char)]].draw(FlattenPen(glyphset, contours, tol, dx=dx))
    for contour in contours:
        pen.moveTo(contour[0])
        for pt in contour[1:]:
            pen.lineTo(pt)
        pen.closePath()
    return path


def layout(text: str, font_name: str, tracking: float):
    """Return (shape, spans, cap, letters) like the reference script."""
    font = load_font(font_name)
    cmap, hmtx = font.getBestCmap(), font["hmtx"]
    upm = font["head"].unitsPerEm
    cap = getattr(font["OS/2"], "sCapHeight", 0) or upm * 0.7
    tol = max(upm / 2000.0, 0.05)
    for ch in text:
        if ch == " ":
            continue
        if ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F:
            raise invalid_params(
                "Control characters are not allowed in a wordmark — "
                "letters, digits, spaces and basic punctuation only."
            )
        if ord(ch) not in cmap:
            raise invalid_params(
                f"Character {ch!r} is not in the {font_name} face — "
                "letters, digits, spaces and basic punctuation only."
            )
    x, spans = 0.0, []
    total = pathops.Path()
    letters: list[pathops.Path | None] = []
    glyph_count = 0
    for ch in text:
        if ch == " ":
            x += upm * 0.3
            spans.append(None)
            letters.append(None)
            continue
        name = cmap[ord(ch)]
        letter = _draw_letter(font, ch, x, tol)
        adv = hmtx[name][0]
        spans.append((x, x + adv))
        letters.append(letter)
        total = pathops.op(total, letter, pathops.PathOp.UNION)
        x += adv + tracking * upm
        glyph_count += 1
    if glyph_count == 0:
        raise invalid_params("The wordmark needs at least one letter (not only spaces).")
    return total, spans, cap, letters


def auto_cuts(text: str) -> list[tuple[int, float, float]]:
    """Deterministic cuts from the text (stylish, not semantic).

    Same recipe as the reference script; indices wrap around the digest so
    long inputs cannot overrun it (the script indexes past 32 bytes for
    texts above ~48 non-space letters).
    """
    h = hashlib.sha256(text.encode()).digest()
    letters = [i for i, c in enumerate(text) if c != " "]
    n = max(1, len(letters) // 3)
    cuts = []
    for k in range(n):
        j = 1 + h[k % 32] % max(1, len(text) - 1)
        cuts.append((j, 0.35 + (h[(k + 8) % 32] % 40) / 100,
                     0.16 + (h[(k + 16) % 32] % 10) / 100))
    return cuts


def expand(path: pathops.Path, width: float) -> pathops.Path:
    """Path grown outward by `width` (stroke of 2*width unioned with the fill)."""
    st = pathops.Path(path)
    st.stroke(2 * width, pathops.LineCap.ROUND_CAP, pathops.LineJoin.ROUND_JOIN, 4)
    st.convertConicsToQuads()
    return pathops.op(path, st, pathops.PathOp.UNION)


def build(text: str, font_name: str, tracking: float,
          cuts: list[tuple[int, float, float]], gap: float,
          mode: str) -> tuple[pathops.Path, list[CutSpec], list[str]]:
    shape, spans, cap, letters = layout(text, font_name, tracking)
    if mode == "knockout":
        # overlap between neighbouring letters flips to background color
        shape = None
        for letter in letters:
            if letter is None:
                continue
            shape = letter if shape is None else pathops.op(shape, letter, pathops.PathOp.XOR)
    elif mode == "layered":
        # each letter sits on top of the previous ones, separated by a thin gap
        shape = None
        for letter in letters:
            if letter is None:
                continue
            if shape is None:
                shape = letter
            else:
                shape = pathops.op(shape, expand(letter, gap * cap),
                                   pathops.PathOp.DIFFERENCE)
                shape = pathops.op(shape, letter, pathops.PathOp.UNION)
    assert shape is not None
    solid = []
    applied: list[CutSpec] = []
    warnings: list[str] = []
    for j, yf, rf in cuts:
        if j <= 0 or j >= len(spans) or not spans[j] or not spans[j - 1]:
            warnings.append("cut_skipped_space")
            continue
        cx = spans[j][0]  # junction = left edge of letter j
        cy, r = yf * cap, rf * cap
        # carve a gap ring, then put the solid core back in
        shape = pathops.op(shape, circle(cx, cy, r + gap * cap),
                           pathops.PathOp.DIFFERENCE)
        solid.append(circle(cx, cy, r))
        applied.append(CutSpec(junction=j, y=yf, r=rf))
    for s in solid:
        shape = pathops.op(shape, s, pathops.PathOp.UNION)
    if len(set(warnings)) > 1 or warnings:
        warnings = sorted(set(warnings))
    return shape, applied, warnings


def to_svg(path: pathops.Path, width: int, title: str) -> tuple[str, float]:
    """Bake the y-flip + width fit into the coordinates (no SVG transform).

    Sibling renderers emit plain coordinates the convert pipeline parses
    without transform handling; a ``scale(1,-1)`` group would work too, but
    baked coordinates keep the file trivially re-parseable. Returns
    (svg_text, height_units).
    """
    x0, y0, x1, y1 = path.bounds
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        raise invalid_params("The wordmark drew nothing — try a longer text.")
    s = width / w
    height = h * s
    pen = SVGPathPen(None)
    path.draw(TransformPen(pen, (s, 0, 0, -s, -x0 * s, y1 * s)))
    d = pen.getCommands()
    comment = f"<!-- {title} — negative-space wordmark; font coords are y-up -->"
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height:.1f}" viewBox="0 0 {width} {height:.1f}">'
        f"{comment}"
        f'<path fill="{_INK}" fill-rule="nonzero" d="{d}"/>'
        "</svg>"
    )
    return svg, height


def render_wordmark(text: str, font: str, mode: str, tracking: float,
                    gap: float, cuts: list[CutSpec] | None,
                    width: int) -> tuple[str, dict[str, int], dict[str, int],
                                        list[CutSpec], list[str]]:
    """Full build: cuts → booleans → SVG. Returns
    (svg_text, path_counts, raw_counts, cuts_applied, warnings)."""
    requested = [(c.junction, c.y, c.r) for c in cuts] if cuts else auto_cuts(text)
    shape, applied, warnings = build(text, font, tracking, requested, gap, mode)
    svg, _height = to_svg(shape, width, f"{text} ({font}, {mode})")
    svg = svgmeta.stamp(svg, title=f"{text} ({font}, {mode})", require_owner=True)
    letters = sum(1 for ch in text if ch != " ")
    path_counts = {"letters": letters, "cuts": len(applied)}
    raw_counts = {"contours": _count_contours(shape), "letters": letters}
    return svg, path_counts, raw_counts, applied, warnings


class _ContourCounter(BasePen):
    """Counts closed contours of a pathops path (stats only)."""

    def __init__(self):
        super().__init__(None)
        self.contours = 0

    def _moveTo(self, pt):
        pass

    def _lineTo(self, pt):
        pass

    def _curveToOne(self, a, b, c):
        pass

    def _qCurveToOne(self, a, b):
        pass

    def _closePath(self):
        self.contours += 1

    def _endPath(self):
        pass


def _count_contours(path: pathops.Path) -> int:
    try:
        counter = _ContourCounter()
        path.draw(counter)
        return counter.contours
    except Exception:
        return -1
