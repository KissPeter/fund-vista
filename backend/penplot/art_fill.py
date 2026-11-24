"""Crop a fill-flagged artwork to the free area of the sheet.

An artwork whose root carries ``data-kp-fit="fill"`` (the traffic render: its
data stops at a fixed box, so it can never be grown to the page) is cropped
around its centre to the aspect of the free area, instead of being letterboxed
into it. The plotter cannot clip, so the strokes are clipped geometrically:
every ``<path d="M.. L..">`` is cut at the crop rectangle and the root viewBox
and size follow. Only simple artwork is cropped (``svg``, ``g``, ``path`` with
absolute ``M``/``L`` commands, ``circle``, ``title``); anything else is left
as it is and keeps the letterbox.
"""

from __future__ import annotations

import re

from backend.traffic.geo import clip_polyline

FILL_ATTR_RE = re.compile(r"""\sdata-kp-fit\s*=\s*(["'])fill\1""")
_ROOT_RE = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)
_VIEWBOX_RE = re.compile(r"""\sviewBox\s*=\s*(["'])([^"']*)\1""")
_PATH_RE = re.compile(r"""<path\s+d="([^"]*)"\s*(?:/>|>(.*?)</path>)""", re.DOTALL)
_CIRCLE_RE = re.compile(r"""<circle\b[^>]*?\bcx="([^"]*)"[^>]*?\bcy="([^"]*)"[^>]*?/>""")
_TAG_RE = re.compile(r"<([A-Za-z][\w:-]*)")
_ALLOWED = {"svg", "g", "path", "circle", "title"}
_NUM = r"-?\d+(?:\.\d+)?"
_D_RE = re.compile(rf"^\s*M\s*{_NUM}[ ,]{_NUM}(?:\s*L\s*{_NUM}[ ,]{_NUM})*\s*$")
_PT_RE = re.compile(rf"({_NUM})[ ,]({_NUM})")
_MM_PER_UNIT = 0.3  # the traffic render's mm per user unit


def is_fill(svg: str) -> bool:
    m = _ROOT_RE.search(svg)
    return bool(m and FILL_ATTR_RE.search(m.group(0)))


def _crop_rect(vb: tuple[float, float, float, float], aspect: float):
    x, y, w, h = vb
    if w / h > aspect:  # too wide -> crop the width
        cw = h * aspect
        return (x + (w - cw) / 2, y, cw, h)
    ch = w / aspect  # too tall -> crop the height
    return (x, y + (h - ch) / 2, w, ch)


def crop_to_aspect(svg: str, aspect: float) -> str:
    """The artwork cropped to ``aspect`` (width/height); unchanged when it cannot be."""
    root = _ROOT_RE.search(svg)
    if root is None or not is_fill(svg) or aspect <= 0:
        return svg
    vbm = _VIEWBOX_RE.search(root.group(0))
    if vbm is None:
        return svg
    try:
        vb = tuple(float(n) for n in re.split(r"[ ,]+", vbm.group(2).strip()))
    except ValueError:
        return svg
    if len(vb) != 4 or vb[2] <= 0 or vb[3] <= 0:
        return svg
    if {t.lower() for t in _TAG_RE.findall(svg)} - _ALLOWED:
        return svg
    if any(not _D_RE.match(m.group(1)) for m in _PATH_RE.finditer(svg)):
        return svg
    cx, cy, cw, ch = _crop_rect(vb, aspect)  # type: ignore[arg-type]
    if abs(cw - vb[2]) < 1e-9 and abs(ch - vb[3]) < 1e-9:
        return svg

    def path(m: re.Match[str]) -> str:
        inner = m.group(2)
        pts = [(float(a) - cx, float(b) - cy) for a, b in _PT_RE.findall(m.group(1))]
        pieces = clip_polyline(pts, cw, ch)
        tail = f">{inner}</path>" if inner is not None else "/>"
        return "".join(
            '<path d="M' + " L".join(f"{x + cx:.1f},{y + cy:.1f}" for x, y in seg) + '"' + tail
            for seg in pieces
        )

    def circle(m: re.Match[str]) -> str:
        x, y = float(m.group(1)), float(m.group(2))
        inside = cx <= x <= cx + cw and cy <= y <= cy + ch
        return m.group(0) if inside else ""

    body = svg[root.end():]
    body = _PATH_RE.sub(path, body)
    body = _CIRCLE_RE.sub(circle, body)
    tag = root.group(0)
    tag = _VIEWBOX_RE.sub(f' viewBox="{cx:.4f} {cy:.4f} {cw:.4f} {ch:.4f}"', tag)
    tag = re.sub(r'\swidth="[^"]*"', f' width="{cw * _MM_PER_UNIT:.4f}mm"', tag)
    tag = re.sub(r'\sheight="[^"]*"', f' height="{ch * _MM_PER_UNIT:.4f}mm"', tag)
    return svg[: root.start()] + tag + body
