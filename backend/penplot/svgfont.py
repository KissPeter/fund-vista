"""Single-stroke SVGinOT face → Hershey-style stroke table.

ZnikoSL-SVGinOT-8 (glukfonts, SIL OFL 1.1) is a single-line plotter font in
the OpenType-SVG format: every glyph is one ``<svg>`` document of bare
``<path>`` strokes (no fills), y-negative-up, roughly on a 1000-unit em
with the baseline at y=0. PIL/FreeType cannot rasterize SVG glyphs, so we
parse the path data directly into polylines — exact single strokes, no
tracing, no thinning.

The built face mirrors the ``hershey_fonts.FACES`` entry shape
(``cap_height``, ``space_advance``, ``glyphs`` with ``advance`` + ``lines``
in face units, y-down from the cap top), so ``labels._resolve_face`` and
the stats-table text layout consume it unchanged. Built once per process
and cached (575 glyphs parse in milliseconds).
"""

from __future__ import annotations

import math
import os
import re

from fontTools.ttLib import TTFont

FONT_ID = "znikoslsvginot"
FONT_FILE = "Znikoslsvginot8-GOB3y.ttf"

ATTRIBUTION_SINGLESTROKE = (
    "ZnikoSL-SVGinOT-8 (c) Grzegorz Luka (glukfonts.pl), SIL OFL 1.1; "
    "single-stroke SVGinOT, TTF vendored under backend/penplot/fonts/."
)

#: Flattening tolerance for bezier segments, in font units (upm 1000).
_FLATTEN_TOL = 0.5

_face: dict | None = None


def _font_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", FONT_FILE)


def _tokenize(d: str) -> list[str]:
    return re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|-?\d*\.?\d+(?:[eE][-+]?\d+)?", d)


def _flatten_cubic(p0, p1, p2, p3, tol: float) -> list[tuple[float, float]]:
    """Recursive-subdivision flattening (keeps endpoints, drops dup joints)."""
    mx1 = (p0[0] + p1[0]) / 2.0
    my1 = (p0[1] + p1[1]) / 2.0
    mx2 = (p1[0] + p2[0]) / 2.0
    my2 = (p1[1] + p2[1]) / 2.0
    mx3 = (p2[0] + p3[0]) / 2.0
    my3 = (p2[1] + p3[1]) / 2.0
    mx12 = (mx1 + mx2) / 2.0
    my12 = (my1 + my2) / 2.0
    mx23 = (mx2 + mx3) / 2.0
    my23 = (my2 + my3) / 2.0
    cx, cy = (mx12 + mx23) / 2.0, (my12 + my23) / 2.0
    lx, ly = p3[0] - p0[0], p3[1] - p0[1]
    dist = abs(lx * (p0[1] - cy) - ly * (p0[0] - cx)) / max(math.hypot(lx, ly), 1e-9)
    if dist <= tol:
        return [p3]
    left = _flatten_cubic(p0, (mx1, my1), (mx12, my12), (cx, cy), tol)
    right = _flatten_cubic((cx, cy), (mx23, my23), (mx3, my3), p3, tol)
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


def _arc_to_lines(p0, rx, ry, rot_deg, large: int, sweep: int, p1,
                  tol: float) -> list[tuple[float, float]]:
    """SVG endpoint arcs -> polyline (F.6.5 endpoint-to-center conversion)."""
    if rx < 1e-9 or ry < 1e-9 or p0 == p1:
        return [p1]
    phi = math.radians(rot_deg % 360.0)
    dx, dy = (p0[0] - p1[0]) / 2.0, (p0[1] - p1[1]) / 2.0
    x1p = math.cos(phi) * dx + math.sin(phi) * dy
    y1p = -math.sin(phi) * dx + math.cos(phi) * dy
    lam = x1p * x1p / (rx * rx) + y1p * y1p / (ry * ry)
    if lam > 1.0:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    f = math.sqrt(max(num / max(den, 1e-12), 0.0))
    if large == sweep:
        f = -f
    cxp, cyp = f * rx * y1p / ry, -f * ry * x1p / rx
    cx = math.cos(phi) * cxp - math.sin(phi) * cyp + (p0[0] + p1[0]) / 2.0
    cy = math.sin(phi) * cxp + math.cos(phi) * cyp + (p0[1] + p1[1]) / 2.0

    def angle(ux: float, uy: float, vx: float, vy: float) -> float:
        d = math.hypot(ux, uy) * math.hypot(vx, vy)
        c = max(-1.0, min(1.0, (ux * vx + uy * vy) / max(d, 1e-12)))
        a = math.acos(c)
        return -a if ux * vy - uy * vx < 0.0 else a

    t1 = angle(1.0, 0.0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = angle((x1p - cxp) / rx, (y1p - cyp) / ry,
               (-x1p - cxp) / rx, (-y1p - cyp) / ry) % (2.0 * math.pi)
    if sweep == 0 and dt > 1e-9:
        dt -= 2.0 * math.pi
    if sweep == 1 and dt < -1e-9:
        dt += 2.0 * math.pi
    steps = max(2, int(abs(dt) / (math.pi / 45.0)) + 1)
    pts = []
    for i in range(1, steps + 1):
        a = t1 + dt * i / steps
        ex, ey = rx * math.cos(a), ry * math.sin(a)
        pts.append((cx + math.cos(phi) * ex - math.sin(phi) * ey,
                    cy + math.sin(phi) * ex + math.cos(phi) * ey))
    return pts


def _parse_path(d: str, tol: float) -> list[list[tuple[float, float]]]:
    """One SVG ``d`` -> list of polylines (subpaths stay separate strokes)."""
    tokens = _tokenize(d)
    strokes: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    x = y = 0.0
    sx = sy = 0.0
    cmd = ""
    last_cubic: tuple[float, float] | None = None
    last_quad: tuple[float, float] | None = None
    i, n = 0, len(tokens)

    def isnum(t: str) -> bool:
        return t not in "MmLlHhVvCcSsQqTtAaZz"

    def flush() -> None:
        if len(cur) > 1:
            strokes.append(cur)

    def emit(px: float, py: float) -> None:
        nonlocal x, y, cur
        x, y = px, py
        if not cur:
            cur.append((x, y))
        elif math.hypot(x - cur[-1][0], y - cur[-1][1]) > 1e-9:
            cur.append((x, y))

    while i < n:
        t = tokens[i]
        if not isnum(t):
            cmd = t
            i += 1
            if cmd in "Zz":
                if cur:
                    if math.hypot(cur[0][0] - x, cur[0][1] - y) > 1e-9:
                        cur.append((sx, sy))
                    flush()
                    cur = []
                x, y = sx, sy
                last_cubic = last_quad = None
                continue
        nums: list[float] = []
        while i < n and isnum(tokens[i]):
            nums.append(float(tokens[i]))
            i += 1
        k = 0
        while k < len(nums):
            if cmd == "M":
                # Moveto always starts a new subpath.
                flush()
                cur = []
                emit(nums[k], nums[k + 1])
                sx, sy = x, y
                cmd = "L"
                k += 2
            elif cmd == "m":
                flush()
                cur = []
                emit(x + nums[k], y + nums[k + 1])
                sx, sy = x, y
                cmd = "l"
                k += 2
            elif cmd == "L":
                emit(nums[k], nums[k + 1])
                k += 2
            elif cmd == "l":
                emit(x + nums[k], y + nums[k + 1])
                k += 2
            elif cmd == "H":
                emit(nums[k], y)
                k += 1
            elif cmd == "h":
                emit(x + nums[k], y)
                k += 1
            elif cmd == "V":
                emit(x, nums[k])
                k += 1
            elif cmd == "v":
                emit(x, y + nums[k])
                k += 1
            elif cmd == "C":
                p0 = (x, y)
                p1, p2, p3 = ((nums[k], nums[k + 1]), (nums[k + 2], nums[k + 3]),
                              (nums[k + 4], nums[k + 5]))
                if not cur:
                    cur.append(p0)
                for pt in _flatten_cubic(p0, p1, p2, p3, tol):
                    emit(*pt)
                last_cubic, last_quad = p2, None
                k += 6
            elif cmd == "c":
                p0 = (x, y)
                p1 = (x + nums[k], y + nums[k + 1])
                p2 = (x + nums[k + 2], y + nums[k + 3])
                p3 = (x + nums[k + 4], y + nums[k + 5])
                if not cur:
                    cur.append(p0)
                for pt in _flatten_cubic(p0, p1, p2, p3, tol):
                    emit(*pt)
                last_cubic, last_quad = p2, None
                k += 6
            elif cmd == "S":
                p0 = (x, y)
                p1 = (2.0 * x - last_cubic[0], 2.0 * y - last_cubic[1]) \
                    if last_cubic is not None else p0
                p2, p3 = ((nums[k], nums[k + 1]), (nums[k + 2], nums[k + 3]))
                if not cur:
                    cur.append(p0)
                for pt in _flatten_cubic(p0, p1, p2, p3, tol):
                    emit(*pt)
                last_cubic, last_quad = p2, None
                k += 4
            elif cmd == "s":
                p0 = (x, y)
                p1 = (2.0 * x - last_cubic[0], 2.0 * y - last_cubic[1]) \
                    if last_cubic is not None else p0
                p2 = (x + nums[k], y + nums[k + 1])
                p3 = (x + nums[k + 2], y + nums[k + 3])
                if not cur:
                    cur.append(p0)
                for pt in _flatten_cubic(p0, p1, p2, p3, tol):
                    emit(*pt)
                last_cubic, last_quad = p2, None
                k += 4
            elif cmd == "Q":
                p0 = (x, y)
                p1, p3 = ((nums[k], nums[k + 1]), (nums[k + 2], nums[k + 3]))
                if not cur:
                    cur.append(p0)
                for pt in _flatten_quad(p0, p1, p3, tol):
                    emit(*pt)
                last_quad, last_cubic = p1, None
                k += 4
            elif cmd == "q":
                p0 = (x, y)
                p1 = (x + nums[k], y + nums[k + 1])
                p3 = (x + nums[k + 2], y + nums[k + 3])
                if not cur:
                    cur.append(p0)
                for pt in _flatten_quad(p0, p1, p3, tol):
                    emit(*pt)
                last_quad, last_cubic = p1, None
                k += 4
            elif cmd == "T":
                p0 = (x, y)
                p1 = (2.0 * x - last_quad[0], 2.0 * y - last_quad[1]) \
                    if last_quad is not None else p0
                p3 = (nums[k], nums[k + 1])
                if not cur:
                    cur.append(p0)
                for pt in _flatten_quad(p0, p1, p3, tol):
                    emit(*pt)
                last_quad, last_cubic = p1, None
                k += 2
            elif cmd == "t":
                p0 = (x, y)
                p1 = (2.0 * x - last_quad[0], 2.0 * y - last_quad[1]) \
                    if last_quad is not None else p0
                p3 = (x + nums[k], y + nums[k + 1])
                if not cur:
                    cur.append(p0)
                for pt in _flatten_quad(p0, p1, p3, tol):
                    emit(*pt)
                last_quad, last_cubic = p1, None
                k += 2
            elif cmd == "A":
                p0 = (x, y)
                rx, ry, rot = nums[k], nums[k + 1], nums[k + 2]
                large, sweep = int(nums[k + 3]), int(nums[k + 4])
                p3 = (nums[k + 5], nums[k + 6])
                if not cur:
                    cur.append(p0)
                for pt in _arc_to_lines(p0, rx, ry, rot, large, sweep, p3, tol):
                    emit(*pt)
                last_cubic = last_quad = None
                k += 7
            elif cmd == "a":
                p0 = (x, y)
                rx, ry, rot = nums[k], nums[k + 1], nums[k + 2]
                large, sweep = int(nums[k + 3]), int(nums[k + 4])
                p3 = (x + nums[k + 5], y + nums[k + 6])
                if not cur:
                    cur.append(p0)
                for pt in _arc_to_lines(p0, rx, ry, rot, large, sweep, p3, tol):
                    emit(*pt)
                last_cubic = last_quad = None
                k += 7
            else:  # pragma: no cover - unknown command, skip its run
                break
    # Note: no flush between command runs — a subpath routinely spans an
    # M run and the L run that extends it; only M/Z/end terminate one.
    flush()
    return strokes


def get_svg_face() -> dict:
    """Built ``znikoslsvginot`` face, cached per process.

    Returns a ``hershey_fonts.FACES``-shaped entry: ``cap_height`` (measured
    off the ``H`` glyph), ``space_advance`` (hmtx space), and ``glyphs``
    mapping characters to ``{"advance", "lines"}`` in face units, y-down
    from the cap top. Glyphs that parse empty are absent so callers warn
    uniformly (same as Hershey misses).
    """
    global _face
    if _face is not None:
        return _face
    from fontTools.ttLib import TTFont

    font = TTFont(_font_path())
    order = font.getGlyphOrder()
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    docs = font["SVG "].docList

    def doc_for(gid: int) -> str | None:
        for doc in docs:
            if doc.startGlyphID <= gid <= doc.endGlyphID:
                return doc.data
        return None

    raw: dict[str, tuple[float, list[list[tuple[float, float]]]]] = {}
    for code, glyph_name in cmap.items():
        ch = chr(code)
        if ch in raw:
            continue
        try:
            gid = order.index(glyph_name)
        except ValueError:
            continue
        data = doc_for(gid)
        if not data:
            continue
        strokes: list[list[tuple[float, float]]] = []
        for m in re.finditer(r"<path[^>]*d=\"([^\"]+)\"", data):
            try:
                strokes.extend(_parse_path(m.group(1), _FLATTEN_TOL))
            except (IndexError, ValueError):
                continue
        if not strokes:
            continue
        adv = float(hmtx[glyph_name][0])
        raw[ch] = (adv, strokes)

    # Cap top measured off H; everything shifts so cap top is y=0, y-down.
    h_strokes = raw.get("H", (0.0, []))[1]
    cap_ys = [y for line in h_strokes for _, y in line]
    cap_top = min(cap_ys) if cap_ys else -760.0
    cap_bottom_candidates = [y for line in h_strokes for _, y in line]
    cap_height = (max(cap_bottom_candidates) - cap_top) if cap_bottom_candidates else 755.0

    glyphs: dict[str, dict] = {}
    for ch, (adv, strokes) in raw.items():
        glyphs[ch] = {
            "advance": adv,
            "lines": [[(x, y - cap_top) for x, y in line] for line in strokes],
        }
    space_adv = float(hmtx[cmap[32]][0]) if 32 in cmap else 400.0
    _face = {"cap_height": cap_height, "space_advance": space_adv, "glyphs": glyphs}
    return _face
