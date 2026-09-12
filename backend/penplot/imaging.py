"""Pixel-level helpers: format detection, grayscale load, blur/threshold, SVG parse.

Only permissive-licensed libraries are used here (Pillow HPND, NumPy BSD,
OpenCV Apache-2.0). Deliberately NOT used, per spec §7 licence warning:
Potrace (GPL-2.0) and vpype-flow-imager (GPL-3.0) stay out of the backend so a
future closed-source distribution has no copyleft spill. Contour tracing uses
OpenCV ``findContours``; flow uses our own deterministic field (see methods).
"""

from __future__ import annotations

import io
import logging
import math
import re
import xml.etree.ElementTree as ET

import cv2
import numpy as np
from defusedxml import ElementTree as DefusedET
from defusedxml import DefusedXmlException
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
from PIL.Image import DecompressionBombError

from backend.penplot.config import ALLOWED_RASTER_EXTS
from backend.penplot.errors import PenPlotError, ErrorCode, image_too_large

log = logging.getLogger(__name__)

# Explicit decompression limit (review D.3.2). Pillow errors out past this
# pixel count BEFORE touching the pixel buffer, so a ≤10 MB file can never
# allocate gigabytes. The value is deliberately generous (a 40+ MP DSLR photo
# must still load so the pipeline can downscale it) — the real guard is the
# explicit error path below, not a tight cap.
Image.MAX_IMAGE_PIXELS = 100_000_000

# SVG structural guards (review D.3.3): defusedxml blocks entity-expansion /
# external-reference bombs; these caps bound tree size and nesting depth so a
# few-KB hostile file can't grow unbounded CPU/RAM or hit the 1000-frame Python
# recursion limit inside _walk_children.
_MAX_SVG_ELEMENTS = 200_000
_MAX_SVG_DEPTH = 256

# Warning surfaced when an SVG embeds raster <image> content that is skipped
# (review C.2.5) — makes the §B.4 "never silently wrong" claim hold for the
# mixed-vector/raster case, not just the all-dropped case.
WARNING_EMBEDDED_RASTERS_IGNORED = "embedded_rasters_ignored"

EXT_TO_PILLOW_FORMAT = {
    "png": "PNG",
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "webp": "WEBP",
    "bmp": "BMP",
    "tif": "TIFF",
    "tiff": "TIFF",
}


def looks_like_svg(data: bytes) -> bool:
    head = data.lstrip()[:2048].lower()
    return b"<svg" in head


def sniff_extension(data: bytes, filename: str | None) -> str | None:
    """Return a normalized extension (no dot) or None if unsupported."""
    if looks_like_svg(data):
        return "svg"
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").lower()
    except DecompressionBombError as exc:
        raise image_too_large(f"Image decompresses to too many pixels ({Image.MAX_IMAGE_PIXELS // 1_000_000} MP limit).") from exc
    except UnidentifiedImageError:
        return None
    # Pillow reports "JPEG" for .jpg; normalize both ways.
    if fmt in ALLOWED_RASTER_EXTS:
        return "jpeg" if fmt == "jpg" else fmt
    # Fall back to the client filename hint for odd containers.
    if filename and "." in filename:
        hint = filename.rsplit(".", 1)[-1].lower()
        if hint in ALLOWED_RASTER_EXTS:
            return "jpeg" if hint == "jpg" else hint
    return None


def load_raster(data: bytes) -> tuple[np.ndarray, int, int, str]:
    """Return (gray HxW uint8, width, height, format). Raises PenPlotError(400/413)."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "PNG").lower()
            img = img.convert("RGB")
            gray_img = img.convert("L")
            w, h = gray_img.size
            gray = np.asarray(gray_img, dtype=np.uint8)
    except DecompressionBombError as exc:
        # Review D.3.2: Pillow's bomb error subclasses bare Exception, so a
        # naive `except Exception` here must not swallow it into a blank 500 —
        # surface it as a clean 413 envelope instead.
        raise image_too_large(f"Image decompresses to too many pixels ({Image.MAX_IMAGE_PIXELS // 1_000_000} MP limit).") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise PenPlotError(
            status=400, code=ErrorCode.BAD_IMAGE,
            message="Uploaded file is not a readable image.",
        ) from exc
    return gray, w, h, fmt


def probe_raster(data: bytes) -> tuple[int, int, str]:
    _, w, h, fmt = load_raster(data)
    return w, h, fmt


def maybe_downscale(
    gray: np.ndarray, max_dim_px: int
) -> tuple[np.ndarray, bool]:
    """Downscale longest side to max_dim_px (AREA filter). Returns (img, scaled)."""
    h, w = gray.shape[:2]
    longest = max(h, w)
    if longest <= max_dim_px:
        return gray, False
    scale = max_dim_px / float(longest)
    new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
    resized = cv2.resize(gray, (new_w, new_h), interpolation=cv2.INTER_AREA)
    log.debug("downscaled %dx%d -> %dx%d", w, h, new_w, new_h)
    return resized, True


def blur(gray: np.ndarray, radius: float) -> np.ndarray:
    if radius <= 0:
        return gray
    # radius (sigma-ish) -> odd kernel; cap kernel at 31 for CPU safety.
    k = max(3, int(radius * 3) | 1)
    k = min(k, 31)
    return cv2.GaussianBlur(gray, (k, k), sigmaX=radius)


def adjust_contrast(gray: np.ndarray, amount: float) -> np.ndarray:
    """Linear contrast stretch around mid-gray: 1.0 is identity.

    amount > 1 pushes darks darker and lights lighter (low-contrast detail
    separates from its background before thresholding); 0 flattens everything
    to mid-gray. Runs after blur, before the threshold mask, so all raster
    methods (contour/hatch/flow) benefit equally.
    """
    if amount == 1.0:
        return gray
    stretched = (gray.astype(np.float32) - 128.0) * float(amount) + 128.0
    return np.clip(stretched, 0.0, 255.0).astype(np.uint8)


def adjust_brightness(gray: np.ndarray, amount: float) -> np.ndarray:
    """Additive brightness offset: 0 is identity, + brightens, − darkens.

    Runs after contrast, before the threshold mask, so all raster methods
    (contour/hatch/flow) benefit equally. Clipped to 8-bit range.
    """
    if amount == 0.0:
        return gray
    shifted = gray.astype(np.float32) + float(amount)
    return np.clip(shifted, 0.0, 255.0).astype(np.uint8)


def remove_background(gray: np.ndarray) -> np.ndarray:
    """Flatten uneven paper background (vignette, shadows, gray paper).

    Estimates the background with a heavy blur on a thumbnail (fast even on
    3000 px uploads), subtracts it (paper → 0, ink stays positive), normalizes
    to full range and inverts back to dark-ink-on-white semantics so the
    downstream blur/contrast/threshold chain behaves exactly as before —
    just with the paper gradient gone. Uniform blanks map to all-white.
    """
    h, w = gray.shape[:2]
    longest = max(h, w)
    thumb_long = 96
    scale = thumb_long / float(max(longest, 1))
    tw, th = max(1, int(w * scale)), max(1, int(h * scale))
    small = cv2.resize(gray, (tw, th), interpolation=cv2.INTER_AREA)
    bg_small = cv2.GaussianBlur(small, (0, 0), sigmaX=max(th, tw) / 8.0)
    bg = cv2.resize(bg_small, (w, h), interpolation=cv2.INTER_LINEAR)
    fg = cv2.subtract(bg, gray)  # saturating: paper ~0, ink positive
    norm = cv2.normalize(fg, None, 0, 255, cv2.NORM_MINMAX)
    return (255 - norm).astype(np.uint8)


def threshold_mask(gray: np.ndarray, threshold: int) -> np.ndarray:
    """Binary ink mask: True where pixel is darker than threshold."""
    return gray < np.uint8(threshold)


def strip_hatch(mask: np.ndarray, kernel_px: int) -> np.ndarray:
    """Erase ink strokes thinner than ``kernel_px`` via morphological opening.

    Crosshatch/hatch fill is made of thin, closely-spaced parallel strokes;
    real linework (outlines, bold glyphs, solid fills) is thicker. Opening
    (erode then dilate) with an isotropic kernel removes any connected ink
    region thinner than the kernel in every direction and regenerates the
    rest at full width — so hatch strokes vanish, whether they sit in
    decorative background or inside a shape's own shading, while thicker
    strokes survive intact.

    This is deliberately texture-agnostic: unlike ``remove_background`` (a
    flat-field/vignette corrector for uneven paper tone), it never asks
    whether a region is "background" or "foreground" — only stroke width
    decides. Pick ``kernel_px`` just above the hatch line width and below
    the thinnest stroke you want to keep; 0 disables (identity).
    """
    kernel_px = max(0, int(kernel_px))
    if kernel_px <= 0:
        return mask
    k = max(3, kernel_px | 1)  # odd, >= 3 for a valid structuring element
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    ink = (mask.astype(np.uint8)) * 255
    opened = cv2.morphologyEx(ink, cv2.MORPH_OPEN, kernel)
    return opened > 0


def a4_dpi(width_px: int, a4_width_mm: float = 210.0) -> float:
    return width_px / (a4_width_mm / 25.4)


# -- SVG vector reading (vpype `read` equivalent) ---------------------------
#
# Covers: full path command set (M/L/H/V/C/S/Q/T/A/Z, absolute+relative),
# rect (incl. rounded corners), circle, ellipse, line, polyline, polygon,
# <text> (outlined via Pillow raster + contour tracing), nested transform=
# attributes, viewBox + preserveAspectRatio viewport mapping, CSS length
# units, display/visibility filtering, and <use> references.
# Known non-goals (all surfaced, not silent): embedded <image> rasters raise
# the `embedded_rasters_ignored` warning, paint servers (gradients/patterns —
# outlines only) and external refs (blocked by defusedxml) are dropped.

_NUMBER_RE = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"

# CSS absolute length units in px (96 dpi reference).
_UNIT_TO_PX = {
    "": 1.0, "px": 1.0,
    "in": 96.0, "cm": 96.0 / 2.54, "mm": 96.0 / 25.4, "q": 96.0 / 25.4 / 40.0,
    "pt": 96.0 / 72.0, "pc": 16.0,
}
_LENGTH_RE = re.compile(rf"^\s*({_NUMBER_RE})\s*([a-zA-Z%]*)\s*$")


def _parse_length(value: str | None, default: float, pct_ref: float = 0.0) -> float:
    """Parse a CSS length to px. ``%`` resolves against ``pct_ref``."""
    if not value:
        return default
    m = _LENGTH_RE.match(value)
    if not m:
        return default
    try:
        num = float(m.group(1))
    except ValueError:
        return default
    unit = m.group(2).lower()
    if unit == "%":
        return num / 100.0 * pct_ref if pct_ref > 0 else default
    if unit in _UNIT_TO_PX:
        return num * _UNIT_TO_PX[unit]
    return default


def _first_number(value: str | None, default: float) -> float:
    if not value:
        return default
    m = re.search(_NUMBER_RE, value)
    if not m:
        return default
    try:
        return float(m.group(0))
    except ValueError:
        return default


def _style_decls(el: ET.Element) -> dict[str, str]:
    style = el.attrib.get("style", "")
    out: dict[str, str] = {}
    for decl in style.split(";"):
        if ":" in decl:
            key, val = decl.split(":", 1)
            out[key.strip().lower()] = val.strip().lower()
    return out


def _element_hidden(el: ET.Element) -> bool:
    if el.attrib.get("display", "").strip().lower() == "none":
        return True
    if el.attrib.get("visibility", "").strip().lower() in ("hidden", "collapse"):
        return True
    decls = _style_decls(el)
    if decls.get("display") == "none":
        return True
    return decls.get("visibility") in ("hidden", "collapse")


# -- 2D affine transforms ----------------------------------------------------
# Stored as (a, b, c, d, e, f): (x, y) -> (a*x + c*y + e, b*x + d*y + f).

Matrix = tuple[float, float, float, float, float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mmul(outer: Matrix, inner: Matrix) -> Matrix:
    """Compose: apply ``inner`` first, then ``outer``."""
    oa, ob, oc, od, oe, of_ = outer
    ia, ib, ic, id_, ie, if_ = inner
    return (
        oa * ia + oc * ib,
        ob * ia + od * ib,
        oa * ic + oc * id_,
        ob * ic + od * id_,
        oa * ie + oc * if_ + oe,
        ob * ie + od * if_ + of_,
    )


def _mapply(m: Matrix, pt: tuple[float, float]) -> tuple[float, float]:
    a, b, c, d, e, f = m
    x, y = pt
    return (a * x + c * y + e, b * x + d * y + f)


_TRANSFORM_RE = re.compile(r"(\w+)\s*\(([^)]*)\)")


def _parse_transform(value: str | None) -> Matrix:
    """Parse an SVG transform attribute (identity when absent/garbled)."""
    if not value:
        return _IDENTITY
    result = _IDENTITY
    for name, args_raw in _TRANSFORM_RE.findall(value):
        try:
            args = [float(n) for n in re.findall(_NUMBER_RE, args_raw)]
        except ValueError:
            continue
        name = name.strip()
        local: Matrix | None = None
        if name == "translate":
            if len(args) >= 1:
                local = (1, 0, 0, 1, args[0], args[1] if len(args) > 1 else 0.0)
        elif name == "scale":
            if len(args) >= 1:
                local = (args[0], 0, 0, args[1] if len(args) > 1 else args[0], 0, 0)
        elif name == "rotate":
            if len(args) >= 1:
                ang = math.radians(args[0])
                cos_a, sin_a = math.cos(ang), math.sin(ang)
                rot: Matrix = (cos_a, sin_a, -sin_a, cos_a, 0, 0)
                if len(args) >= 3:
                    cx, cy = args[1], args[2]
                    local = _mmul((1, 0, 0, 1, cx, cy),
                                  _mmul(rot, (1, 0, 0, 1, -cx, -cy)))
                else:
                    local = rot
        elif name == "skewX":
            if len(args) >= 1:
                local = (1, 0, math.tan(math.radians(args[0])), 1, 0, 0)
        elif name == "skewY":
            if len(args) >= 1:
                local = (1, math.tan(math.radians(args[0])), 0, 1, 0, 0)
        elif name == "matrix":
            if len(args) >= 6:
                local = (args[0], args[1], args[2], args[3], args[4], args[5])
        if local is not None:
            result = _mmul(result, local)  # lists apply left-to-right
    return result


def _parse_viewbox(value: str | None) -> tuple[float, float, float, float] | None:
    if not value:
        return None
    try:
        nums = [float(n) for n in re.findall(_NUMBER_RE, value)]
    except ValueError:
        return None
    if len(nums) != 4 or nums[2] <= 0 or nums[3] <= 0:
        return None
    return (nums[0], nums[1], nums[2], nums[3])


def _viewbox_ctm(
    minx: float, miny: float, vbw: float, vbh: float,
    vp_w: float, vp_h: float, par: str | None,
) -> Matrix:
    """Map a viewBox rect onto the viewport, honouring preserveAspectRatio."""
    if vbw <= 0 or vbh <= 0 or vp_w <= 0 or vp_h <= 0:
        return _IDENTITY
    parts = [p for p in (par or "").split() if p.lower() != "defer"]
    align = parts[0] if parts else "xMidYMid"
    meet_or_slice = parts[1].lower() if len(parts) > 1 else "meet"
    if align.lower() == "none":
        sx, sy = vp_w / vbw, vp_h / vbh
        return (sx, 0, 0, sy, -minx * sx, -miny * sy)
    m = re.match(r"(?i)^x(min|mid|max)y(min|mid|max)$", align)
    fx = {"min": 0.0, "mid": 0.5, "max": 1.0}[(m.group(1) if m else "mid").lower()]
    fy = {"min": 0.0, "mid": 0.5, "max": 1.0}[(m.group(2) if m else "mid").lower()]
    s = min(vp_w / vbw, vp_h / vbh) if meet_or_slice != "slice" else max(vp_w / vbw, vp_h / vbh)
    return (s, 0, 0, s, -minx * s + (vp_w - vbw * s) * fx, -miny * s + (vp_h - vbh * s) * fy)


_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

# Structural/metadata elements: never rendered directly (only via <use>).
# Embedded <image> rasters are handled explicitly (with a warning) in
# _walk_children, so they are deliberately NOT in this set.
_SKIP_TAGS = frozenset({
    "defs", "symbol", "clippath", "mask", "pattern", "style",
    "metadata", "title", "desc", "script",
})


def _local_tag(el: ET.Element) -> str:
    return el.tag.split("}")[-1].lower() if isinstance(el.tag, str) else ""


def _emit(
    out: list[list[tuple[float, float]]],
    ctm: Matrix,
    polys: list[list[tuple[float, float]]],
) -> None:
    for pl in polys:
        if len(pl) >= 2:
            out.append([_mapply(ctm, pt) for pt in pl])


def _rounded_rect(x: float, y: float, w: float, h: float, rx: float, ry: float) -> list[tuple[float, float]]:
    rx, ry = min(max(rx, 0.0), w / 2.0), min(max(ry, 0.0), h / 2.0)
    if rx <= 0 or ry <= 0:
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
    pts: list[tuple[float, float]] = []
    # Straight edges linked by sampled quarter-arcs (y-down angles).
    corners = [
        ((x + w - rx, y + ry), -90.0, 0.0),    # top-right
        ((x + w - rx, y + h - ry), 0.0, 90.0),  # bottom-right
        ((x + rx, y + h - ry), 90.0, 180.0),    # bottom-left
        ((x + rx, y + ry), 180.0, 270.0),       # top-left
    ]
    pts.append((x + rx, y))
    for (cx, cy), a0, a1 in corners:
        edge_end: tuple[float, float] | None = None
        if a0 == -90.0:
            edge_end = (x + w - rx, y)
        elif a0 == 0.0:
            edge_end = (x + w, y + ry)
        elif a0 == 90.0:
            edge_end = (x + w - rx, y + h)
        else:
            edge_end = (x, y + h - ry)
        if dist2(pts[-1], edge_end) > 1e-12:
            pts.append(edge_end)
        for s in range(7):
            ang = math.radians(a0 + (a1 - a0) * s / 6.0)
            pts.append((cx + rx * math.cos(ang), cy + ry * math.sin(ang)))
    pts.append((x + rx, y))
    return pts


def dist2(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _render_shape(
    el: ET.Element, t: str, ctm: Matrix,
    out: list[list[tuple[float, float]]],
) -> None:
    a = el.attrib
    if t == "path" and a.get("d"):
        _emit(out, ctm, _flatten_path_d(a["d"]))
    elif t == "rect":
        x = _parse_length(a.get("x"), 0.0)
        y = _parse_length(a.get("y"), 0.0)
        w = _parse_length(a.get("width"), 0.0)
        h = _parse_length(a.get("height"), 0.0)
        if w > 0 and h > 0:
            rx_raw, ry_raw = a.get("rx"), a.get("ry")
            rx = _parse_length(rx_raw, 0.0) if rx_raw is not None else None
            ry = _parse_length(ry_raw, 0.0) if ry_raw is not None else None
            if rx is None:
                rx = ry if ry is not None else 0.0
            if ry is None:
                ry = rx
            _emit(out, ctm, [_rounded_rect(x, y, w, h, rx, ry)])
    elif t in ("circle", "ellipse"):
        cx = _parse_length(a.get("cx"), 0.0)
        cy = _parse_length(a.get("cy"), 0.0)
        r_fallback = _parse_length(a.get("r"), 0.0)
        rx_raw, ry_raw = a.get("rx"), a.get("ry")
        rx = _parse_length(rx_raw, r_fallback) if rx_raw is not None else r_fallback
        ry = _parse_length(ry_raw, r_fallback) if ry_raw is not None else r_fallback
        if rx > 0 and ry > 0:
            seg = 48
            _emit(out, ctm, [[
                (cx + rx * math.cos(2 * math.pi * i / seg),
                 cy + ry * math.sin(2 * math.pi * i / seg))
                for i in range(seg + 1)
            ]])
    elif t == "line":
        p1 = (_parse_length(a.get("x1"), 0.0), _parse_length(a.get("y1"), 0.0))
        p2 = (_parse_length(a.get("x2"), 0.0), _parse_length(a.get("y2"), 0.0))
        if dist2(p1, p2) > 1e-12:
            _emit(out, ctm, [[p1, p2]])
    elif t in ("polyline", "polygon"):
        try:
            nums = [float(n) for n in re.findall(_NUMBER_RE, a.get("points", ""))]
        except ValueError:
            return
        pts = [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
        if t == "polygon" and pts:
            pts = pts + [pts[0]]
        _emit(out, ctm, [pts])
    elif t == "text":
        _emit(out, ctm, _text_to_polylines(el))


def _walk_children(
    parent: ET.Element, ctm: Matrix,
    out: list[list[tuple[float, float]]],
    id_map: dict[str, ET.Element], use_stack: tuple[str, ...],
    warnings: list[str],
) -> None:
    for el in parent:
        if not isinstance(el.tag, str):
            continue
        t = _local_tag(el)
        if _element_hidden(el):
            continue
        # Embedded rasters are skipped by design (no raster embedding in a
        # plot file) but must NOT be silent — review C.2.5 (§B.4 claim).
        if t == "image":
            warnings.append(WARNING_EMBEDDED_RASTERS_IGNORED)
            continue
        if t in _SKIP_TAGS:
            continue
        local = _mmul(ctm, _parse_transform(el.attrib.get("transform")))
        if t in ("g", "a", "switch", "symbol"):
            _walk_children(el, local, out, id_map, use_stack, warnings)
        elif t == "svg":  # nested svg: x/y shift + optional own viewport
            nx = _parse_length(el.attrib.get("x"), 0.0)
            ny = _parse_length(el.attrib.get("y"), 0.0)
            sub = _mmul(local, (1, 0, 0, 1, nx, ny))
            nvb = _parse_viewbox(el.attrib.get("viewBox") or el.attrib.get("viewbox"))
            if nvb:
                nw = _parse_length(el.attrib.get("width"), nvb[2], pct_ref=nvb[2])
                nh = _parse_length(el.attrib.get("height"), nvb[3], pct_ref=nvb[3])
                sub = _mmul(
                    sub,
                    _viewbox_ctm(*nvb, nw, nh, el.attrib.get("preserveAspectRatio")),
                )
            _walk_children(el, sub, out, id_map, use_stack, warnings)
        elif t == "use":
            ref = el.attrib.get("href") or el.attrib.get(_XLINK_HREF) or ""
            m = re.match(r"\s*#(.+?)\s*$", ref)
            ux = _parse_length(el.attrib.get("x"), 0.0)
            uy = _parse_length(el.attrib.get("y"), 0.0)
            sub = _mmul(local, (1, 0, 0, 1, ux, uy))
            if m and m.group(1) in id_map and m.group(1) not in use_stack:
                target = id_map[m.group(1)]
                tt = _local_tag(target)
                if tt in ("g", "a", "switch", "symbol", "svg"):
                    _walk_children(
                        target,
                        _mmul(sub, _parse_transform(target.attrib.get("transform"))),
                        out, id_map, use_stack + (m.group(1),), warnings,
                    )
                elif tt == "image":
                    warnings.append(WARNING_EMBEDDED_RASTERS_IGNORED)
                elif tt not in _SKIP_TAGS and not _element_hidden(target):
                    _render_shape(target, tt, sub, out)
        else:
            _render_shape(el, t, local, out)


def parse_svg_vectors(data: bytes) -> tuple[list[list[tuple[float, float]]], float, float, list[str]]:
    """Extract plottable polylines from an SVG (viewport-px space).

    Returns ``(polylines, width_px, height_px, warnings)``. Raises
    PenPlotError(400) when the XML is malformed, contains forbidden entity /
    external references, exceeds the structural caps, or nothing convertible
    is found, so the router reports bad_image instead of a 500.
    """
    try:
        # defusedxml (review D.3.3): raises on DOCTYPE entity/external-ref
        # bombs that the stdlib parser would expand (billion-laughs).
        root = DefusedET.fromstring(data)
    except (ET.ParseError, ValueError, DefusedXmlException) as exc:
        raise PenPlotError(
            status=400, code=ErrorCode.BAD_IMAGE,
            message="Uploaded SVG is not well-formed XML.",
        ) from exc

    # Bounded scan: build the id map while enforcing element-count and
    # nesting-depth caps before any recursive walk (review D.3.3).
    id_map: dict[str, ET.Element] = {}
    count = 0
    max_depth = 0
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        el, depth = stack.pop()
        count += 1
        if depth > max_depth:
            max_depth = depth
        if count > _MAX_SVG_ELEMENTS or max_depth > _MAX_SVG_DEPTH:
            raise PenPlotError(
                status=400, code=ErrorCode.BAD_IMAGE,
                message="SVG is too complex (element count or nesting depth).",
            )
        if isinstance(el.tag, str):
            eid = el.attrib.get("id")
            if eid and eid not in id_map:
                id_map[eid] = el
        for child in el:
            stack.append((child, depth + 1))

    vb = _parse_viewbox(root.attrib.get("viewBox") or root.attrib.get("viewbox"))
    vp_w = _parse_length(root.attrib.get("width"), vb[2] if vb else 210.0,
                         pct_ref=vb[2] if vb else 0.0)
    vp_h = _parse_length(root.attrib.get("height"), vb[3] if vb else 297.0,
                         pct_ref=vb[3] if vb else 0.0)
    if vp_w <= 0:
        vp_w = vb[2] if vb else 210.0
    if vp_h <= 0:
        vp_h = vb[3] if vb else 297.0
    base_ctm = _viewbox_ctm(*vb, vp_w, vp_h,
                            root.attrib.get("preserveAspectRatio")) if vb else _IDENTITY

    polylines: list[list[tuple[float, float]]] = []
    warnings: list[str] = []
    _walk_children(root, base_ctm, polylines, id_map, (), warnings)

    if not polylines:
        raise PenPlotError(
            status=400, code=ErrorCode.BAD_IMAGE,
            message="SVG contains no convertible paths/shapes/text.",
        )
    return polylines, vp_w, vp_h, list(dict.fromkeys(warnings))


# -- path data: full command set -----------------------------------------------

_PATH_TOKEN_RE = re.compile(rf"[MmLlHhVvCcSsQqTtAaZz]|{_NUMBER_RE}")


def _is_cmd(tok: str) -> bool:
    return len(tok) == 1 and tok in "MmLlHhVvCcSsQqTtAaZz"


def _sample_arc(
    p0: tuple[float, float], p1: tuple[float, float],
    rx: float, ry: float, phi_deg: float,
    large_arc: int, sweep: int, max_step_deg: float = 5.0,
) -> list[tuple[float, float]]:
    """Endpoint-parametrized elliptical arc (SVG spec F.6.5), sampled.

    Returns points excluding the start, ending exactly on ``p1`` so chained
    segments and Z-closure stay watertight.
    """
    x0, y0 = p0
    x1, y1 = p1
    rx, ry = abs(rx), abs(ry)
    if rx < 1e-12 or ry < 1e-12 or (abs(x1 - x0) < 1e-12 and abs(y1 - y0) < 1e-12):
        return [p1]
    phi = math.radians(phi_deg % 360.0)
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    dx, dy = (x0 - x1) / 2.0, (y0 - y1) / 2.0
    x1p = cos_p * dx + sin_p * dy
    y1p = -sin_p * dx + cos_p * dy
    radii = x1p**2 / rx**2 + y1p**2 / ry**2
    if radii > 1.0:  # spec F.6.6: scale radii up uniformly until they fit
        s = math.sqrt(radii)
        rx *= s
        ry *= s
    num = rx**2 * ry**2 - rx**2 * y1p**2 - ry**2 * x1p**2
    den = rx**2 * y1p**2 + ry**2 * x1p**2
    # + sign when flags differ, − sign when equal (spec F.6.5).
    sign = -1.0 if large_arc == sweep else 1.0
    coef = sign * math.sqrt(max(0.0, num / den)) if den > 1e-12 else 0.0
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cos_p * cxp - sin_p * cyp + (x0 + x1) / 2.0
    cy = sin_p * cxp + cos_p * cyp + (y0 + y1) / 2.0

    def vec_angle(ux: float, uy: float, vx: float, vy: float) -> float:
        return math.degrees(math.atan2(ux * vy - uy * vx, ux * vx + uy * vy))

    theta1 = vec_angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dtheta = vec_angle(
        (x1p - cxp) / rx, (y1p - cyp) / ry,
        (-x1p - cxp) / rx, (-y1p - cyp) / ry,
    ) % 360.0
    if not sweep:
        dtheta -= 360.0
    steps = max(2, int(math.ceil(abs(dtheta) / max_step_deg)) + 1)
    out = []
    for s in range(1, steps + 1):
        ang = math.radians(theta1 + dtheta * s / steps)
        xp, yp = rx * math.cos(ang), ry * math.sin(ang)
        out.append((cos_p * xp - sin_p * yp + cx, sin_p * xp + cos_p * yp + cy))
    out[-1] = p1
    return out


def _flatten_path_d(d: str, curve_steps: int = 12) -> list[list[tuple[float, float]]]:
    """Flatten path data to polylines — one per subpath (M breaks, Z closes).

    Curves/arcs are sampled; every command (M/L/H/V/C/S/Q/T/A/Z,
    absolute+relative) is supported. Like vpype, each subpath becomes its own
    path so the pen lifts between them instead of drawing connectors.
    """
    tokens: list[str] = _PATH_TOKEN_RE.findall(d.replace(",", " "))
    subs: list[list[tuple[float, float]]] = []
    cur_line: list[tuple[float, float]] = []
    cur = [0.0, 0.0]
    sub_start = [0.0, 0.0]
    cmd = ""
    prev_cubic: tuple[float, float] | None = None  # abs 2nd control of C/S
    prev_quad: tuple[float, float] | None = None   # abs control of Q/T
    i, n = 0, len(tokens)
    started = False  # any geometry emitted yet (first M must not split)

    def flush() -> None:
        """Push the current subpath if drawable; drop degenerate points."""
        if len(cur_line) >= 2:
            subs.append(cur_line.copy())
        cur_line.clear()

    def ensure_started() -> None:
        """A segment command with no current line starts one at the pen."""
        if not cur_line:
            cur_line.append((cur[0], cur[1]))

    def take_number() -> float | None:
        nonlocal i
        if i < n and not _is_cmd(tokens[i]):
            try:
                v = float(tokens[i])
            except ValueError:
                v = None
            i += 1
            return v
        return None

    def take_flag() -> int | None:
        """Arc flags are single 0/1 chars, possibly jammed (e.g. "01")."""
        nonlocal i
        if i >= n or _is_cmd(tokens[i]):
            return None
        tok = tokens[i]
        if tok in ("0", "1"):
            i += 1
            return int(tok)
        if re.fullmatch(r"[01]{2,}", tok or ""):
            tokens[i] = tok[1:]
            return int(tok[0])
        return None

    def sample_cubic(p0: tuple[float, float], p1: tuple[float, float],
                     p2: tuple[float, float], p3: tuple[float, float]) -> None:
        ensure_started()
        for s in range(1, curve_steps + 1):
            t = s / curve_steps
            mt = 1 - t
            cur_line.append((
                mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0],
                mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1],
            ))

    def sample_quad(p0: tuple[float, float], p1: tuple[float, float],
                    p2: tuple[float, float]) -> None:
        ensure_started()
        for s in range(1, curve_steps + 1):
            t = s / curve_steps
            mt = 1 - t
            cur_line.append((
                mt**2 * p0[0] + 2 * mt * t * p1[0] + t**2 * p2[0],
                mt**2 * p0[1] + 2 * mt * t * p1[1] + t**2 * p2[1],
            ))

    while i < n:
        tok = tokens[i]
        if _is_cmd(tok):
            cmd = tok
            i += 1
            if cmd in "Zz":
                if cur_line and dist2(cur_line[-1], (sub_start[0], sub_start[1])) > 1e-9:
                    cur_line.append((sub_start[0], sub_start[1]))
                flush()
                cur = list(sub_start)
                prev_cubic = prev_quad = None
            continue
        if not cmd:
            i += 1  # stray numbers before any command
            continue
        rel = cmd.islower()
        kind = cmd.upper()
        if kind in ("M", "L"):
            vals: list[float] = []
            while (v := take_number()) is not None:
                vals.append(v)
            pairs = [(vals[k], vals[k + 1]) for k in range(0, len(vals) - 1, 2)]
            if kind == "M":
                if started:
                    flush()  # moveto after geometry starts a new subpath
                for j, (x, y) in enumerate(pairs):
                    if rel:
                        x += cur[0]
                        y += cur[1]
                    cur = [x, y]
                    if j == 0:
                        sub_start = list(cur)
                    cur_line.append((x, y))  # rest are implicit lineto, same line
                    started = True
                cmd = "l" if rel else "L"
            else:
                for x, y in pairs:
                    if rel:
                        x += cur[0]
                        y += cur[1]
                    cur = [x, y]
                    ensure_started()
                    cur_line.append((x, y))
                    started = True
            prev_cubic = prev_quad = None
        elif kind == "H":
            while (v := take_number()) is not None:
                x = cur[0] + v if rel else v
                cur = [x, cur[1]]
                ensure_started()
                cur_line.append((x, cur[1]))
                started = True
            prev_cubic = prev_quad = None
        elif kind == "V":
            while (v := take_number()) is not None:
                y = cur[1] + v if rel else v
                cur = [cur[0], y]
                ensure_started()
                cur_line.append((cur[0], y))
                started = True
            prev_cubic = prev_quad = None
        elif kind == "C":
            while True:
                vals = [take_number() for _ in range(6)]
                if any(v is None for v in vals):
                    break
                assert all(v is not None for v in vals)
                p0 = (cur[0], cur[1])
                p1 = (vals[0] + (cur[0] if rel else 0), vals[1] + (cur[1] if rel else 0))
                p2 = (vals[2] + (cur[0] if rel else 0), vals[3] + (cur[1] if rel else 0))
                p3 = (vals[4] + (cur[0] if rel else 0), vals[5] + (cur[1] if rel else 0))
                sample_cubic(p0, p1, p2, p3)
                cur = [p3[0], p3[1]]
                started = True
                prev_cubic = p2
                prev_quad = None
        elif kind == "S":
            while True:
                vals = [take_number() for _ in range(4)]
                if any(v is None for v in vals):
                    break
                assert all(v is not None for v in vals)
                p0 = (cur[0], cur[1])
                if prev_cubic is not None:
                    p1 = (2 * cur[0] - prev_cubic[0], 2 * cur[1] - prev_cubic[1])
                else:
                    p1 = p0
                p2 = (vals[0] + (cur[0] if rel else 0), vals[1] + (cur[1] if rel else 0))
                p3 = (vals[2] + (cur[0] if rel else 0), vals[3] + (cur[1] if rel else 0))
                sample_cubic(p0, p1, p2, p3)
                cur = [p3[0], p3[1]]
                started = True
                prev_cubic = p2
                prev_quad = None
        elif kind == "Q":
            while True:
                vals = [take_number() for _ in range(4)]
                if any(v is None for v in vals):
                    break
                assert all(v is not None for v in vals)
                p0 = (cur[0], cur[1])
                p1 = (vals[0] + (cur[0] if rel else 0), vals[1] + (cur[1] if rel else 0))
                p2 = (vals[2] + (cur[0] if rel else 0), vals[3] + (cur[1] if rel else 0))
                sample_quad(p0, p1, p2)
                cur = [p2[0], p2[1]]
                started = True
                prev_quad = p1
                prev_cubic = None
        elif kind == "T":
            while True:
                vals = [take_number() for _ in range(2)]
                if any(v is None for v in vals):
                    break
                assert all(v is not None for v in vals)
                p0 = (cur[0], cur[1])
                if prev_quad is not None:
                    p1 = (2 * cur[0] - prev_quad[0], 2 * cur[1] - prev_quad[1])
                else:
                    p1 = p0
                p2 = (vals[0] + (cur[0] if rel else 0), vals[1] + (cur[1] if rel else 0))
                sample_quad(p0, p1, p2)
                cur = [p2[0], p2[1]]
                started = True
                prev_quad = p1
                prev_cubic = None
        elif kind == "A":
            while True:
                rx = take_number()
                if rx is None:
                    break
                ry = take_number()
                rot = take_number()
                large = take_flag()
                sweep = take_flag()
                ex = take_number()
                ey = take_number()
                if None in (ry, rot, large, sweep, ex, ey):
                    break  # truncated arc: keep points so far
                assert ry is not None and rot is not None and ex is not None and ey is not None
                assert large is not None and sweep is not None
                if rel:
                    ex += cur[0]
                    ey += cur[1]
                ensure_started()
                for pt in _sample_arc((cur[0], cur[1]), (ex, ey), rx, ry, rot, large, sweep):
                    cur_line.append(pt)
                cur = [ex, ey]
                started = True
                prev_cubic = prev_quad = None
        else:
            break
    flush()
    return subs


# -- <text> to outlines ------------------------------------------------------
# Glyphs are rasterized with Pillow's bundled scalable font, then traced with
# OpenCV contours — no font-engine dependency, permissive licences throughout.

_TEXT_RENDER_EM_PX = 128.0  # raster resolution per em; upscaled geometry, not quality


def _text_runs(el: ET.Element) -> list[tuple[str, float, float]]:
    """Split <text> into (string, x, y) runs, honouring tspan x/y/dx/dy."""
    pen_x = _first_number(el.attrib.get("x"), 0.0) + _first_number(el.attrib.get("dx"), 0.0)
    pen_y = _first_number(el.attrib.get("y"), 0.0) + _first_number(el.attrib.get("dy"), 0.0)
    runs: list[tuple[str, float, float]] = []

    def collapse(s: str | None, preserve: bool) -> str:
        if not s:
            return ""
        if preserve:
            return s
        return re.sub(r"\s+", " ", s).strip(" ")

    preserve = (el.attrib.get("{http://www.w3.org/XML/1998/namespace}space")
                or el.attrib.get("xml:space", "")) == "preserve"

    def push(s: str) -> None:
        s = collapse(s, preserve)
        if s:
            runs.append((s, pen_x, pen_y))

    push(el.text)
    for child in el:
        if not isinstance(child.tag, str):
            continue
        nonlocal_pen = [pen_x, pen_y]
        if _local_tag(child) == "tspan":
            a = child.attrib
            if a.get("x") is not None:
                nonlocal_pen[0] = _first_number(a.get("x"), nonlocal_pen[0])
            if a.get("y") is not None:
                nonlocal_pen[1] = _first_number(a.get("y"), nonlocal_pen[1])
            nonlocal_pen[0] += _first_number(a.get("dx"), 0.0)
            nonlocal_pen[1] += _first_number(a.get("dy"), 0.0)
            s = collapse("".join(child.itertext()), preserve)
            if s:
                runs.append((s, nonlocal_pen[0], nonlocal_pen[1]))
            pen_x, pen_y = nonlocal_pen[0], nonlocal_pen[1]
        else:
            push("".join(child.itertext()))
        push(child.tail)
    return runs


def _text_to_polylines(el: ET.Element) -> list[list[tuple[float, float]]]:
    a = el.attrib
    decls = _style_decls(el)
    font_size = _parse_length(a.get("font-size") or decls.get("font-size"), 16.0, pct_ref=16.0)
    if font_size <= 0:
        return []
    anchor = (a.get("text-anchor") or decls.get("text-anchor") or "start").strip().lower()
    weight = (a.get("font-weight") or decls.get("font-weight") or "").strip().lower()
    faux_bold = weight in ("bold", "bolder", "700", "800", "900")
    stroke_width = 1 if faux_bold else 0

    try:
        font = ImageFont.load_default(size=round(_TEXT_RENDER_EM_PX))
        em_px = _TEXT_RENDER_EM_PX
    except Exception:  # pragma: no cover — ancient Pillow without sized default
        log.warning("text: sized default font unavailable, falling back to bitmap")
        font = ImageFont.load_default()
        ref = font.getbbox("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
        em_px = max(1.0, float(ref[3] - ref[1]) / 0.7)
    uppx = font_size / em_px  # user units per raster px
    try:
        ascent, descent = font.getmetrics()
    except Exception:
        ascent, descent = int(em_px * 0.8), int(em_px * 0.2)

    pad = 8
    out: list[list[tuple[float, float]]] = []
    measure = ImageDraw.Draw(Image.new("L", (8, 8)))
    for s, x, y in _text_runs(el):
        # Advance without stroke: 1 px bearing error at 128 px em is invisible.
        adv_px = measure.textlength(s, font=font)
        if anchor == "middle":
            x -= adv_px * uppx / 2.0
        elif anchor == "end":
            x -= adv_px * uppx
        canvas_w = max(1, int(adv_px) + 2 * pad)
        canvas_h = ascent + descent + 2 * pad
        img = Image.new("L", (canvas_w, canvas_h), 255)
        draw = ImageDraw.Draw(img)
        baseline = pad + ascent  # Pillow y-down == SVG y-down: baseline maps 1:1
        draw.text((pad, baseline), s, font=font, fill=0,
                  anchor="ls", stroke_width=stroke_width)
        mask = (np.asarray(img, dtype=np.uint8) < 128).astype(np.uint8) * 255
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            if cv2.contourArea(cnt) < 2.0:
                continue
            seq = cnt.reshape(-1, 2)
            if len(seq) < 2:
                continue
            out.append([
                (x + (float(px) - pad) * uppx, y + (float(py) - baseline) * uppx)
                for px, py in seq
            ])
    return out
