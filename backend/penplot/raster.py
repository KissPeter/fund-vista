"""Raster ops: format detection, grayscale load, tone cleanup (REF-002 Phase 4).

Pure move from :mod:`backend.penplot.imaging` — identical behavior.
Only permissive-licensed libraries (Pillow HPND, NumPy BSD, OpenCV
Apache-2.0); see the module note in ``svg_vector.py`` for why the SVG
helpers live separately.
"""

from __future__ import annotations

import io
import logging

import cv2
import numpy as np
from defusedxml import ElementTree as DefusedET
from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError

from backend.penplot.config import ALLOWED_RASTER_EXTS
from backend.penplot.errors import ErrorCode, PenPlotError, image_too_large
from backend.penplot.svg_vector import _local_tag

log = logging.getLogger(__name__)

# Explicit decompression limit (review D.3.2). Pillow errors out past this
# pixel count BEFORE touching the pixel buffer, so a ≤25 MB file can never
# allocate gigabytes. The value is deliberately generous (a 40+ MP DSLR photo
# must still load so the pipeline can downscale it) — the real guard is the
# explicit error path below, not a tight cap.
Image.MAX_IMAGE_PIXELS = 100_000_000

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
    """True when the bytes actually parse as an SVG document.

    A plain ``b"<svg"`` substring test is not enough: valid rasters — e.g.
    PNG/JPEG exports whose XMP metadata references ``<svg:...>`` /
    ``xmlns:svg`` — carry that literal inside their first 2 KB and would be
    classified as SVG, then rejected with a bogus "not well-formed XML" 400.
    Verify the bytes parse as XML with an ``<svg>`` root instead.
    """
    head = data.lstrip()[:2048].lower()
    if b"<svg" not in head:
        return False
    try:
        root = DefusedET.fromstring(data)
    except Exception:
        # Deliberate: ANY parse failure means "not an SVG document" (never
        # a 500) — the caller falls through to raster sniffing. Commented,
        # not narrowed, so exotic decoder failures stay a miss, not a crash.
        return False
    return _local_tag(root) == "svg"


def sniff_extension(data: bytes, filename: str | None) -> str | None:
    """Return a normalized extension (no dot) or None if unsupported."""
    # A `.svg` filename wins even for malformed content: routing it to the
    # SVG parser surfaces the honest "not well-formed XML" 400 instead of a
    # confusing 422. Content matches must parse as an actual SVG document
    # (see looks_like_svg) so a raster whose metadata merely mentions `<svg`
    # is not mistaken for a vector.
    name = (filename or "").lower()
    if name.endswith(".svg") or looks_like_svg(data):
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


__all__ = [
    "EXT_TO_PILLOW_FORMAT",
    "a4_dpi",
    "adjust_brightness",
    "adjust_contrast",
    "blur",
    "load_raster",
    "looks_like_svg",
    "maybe_downscale",
    "probe_raster",
    "remove_background",
    "sniff_extension",
    "strip_hatch",
    "threshold_mask",
]
