"""Public PNG answers for cached result drawings.

The public API MUST NOT answer ``image/svg+xml``. A cached result SVG MAY be
rasterized to PNG for a public preview. The vector bytes MUST stay available
only on the internal instance (``PENPLOT_ALLOW_SVG=1``) for the shop server's
order artifacts.

The raster is content-addressed and cached on disk under ``data_dir``. A public
route MUST NOT accept a client-chosen size, colour or path.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
import uuid

from fastapi import Response

from backend.penplot import raster_out
from backend.penplot.config import Settings
from backend.penplot.errors import ErrorCode, PenPlotError

log = logging.getLogger(__name__)

# Every cached result route returns PNG on a public instance and the
# byte-identical SVG only on the loopback instance (PENPLOT_ALLOW_SVG=1).
RESULT_RESPONSES = {
    200: {
        "description": "PNG on public instances; SVG is internal-only (PENPLOT_ALLOW_SVG=1).",
        "content": {"image/png": {}, "image/svg+xml": {}},
    },
}

_RENDER_VERSION = "result-png-v1"
# Unordered derivative retention is 24 h (docs/nfr-retention.md). Expiry is
# lazy: a cached PNG older than this is dropped when it is next read. The
# browser cache must not promise a lifetime longer than the server's bound.
PUBLIC_PNG_TTL_SECONDS = 24 * 60 * 60
PUBLIC_CACHE_CONTROL = f"public, max-age={PUBLIC_PNG_TTL_SECONDS}"
_VIEWBOX_RE = re.compile(r'viewBox\s*=\s*["\']\s*[-\d.eE]+\s+[-\d.eE]+\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)')
_ROOT_RE = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)
_NUMERIC_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _dimensions(svg: str) -> tuple[float, float]:
    """Width and height in the SVG's own user units."""
    viewbox = _VIEWBOX_RE.search(svg)
    if viewbox is not None:
        width, height = float(viewbox.group(1)), float(viewbox.group(2))
        if width > 0 and height > 0:
            return width, height
    root = _ROOT_RE.search(svg)
    if root is None:
        raise PenPlotError(
            status=500,
            code=ErrorCode.PROCESSING_FAILED,
            message="Result has no SVG root.",
        )
    tag = root.group(0)
    values: dict[str, float] = {}
    for name in ("width", "height"):
        match = re.search(rf'\b{name}\s*=\s*["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if match is None:
            continue
        number = _NUMERIC_RE.search(match.group(1))
        if number is not None:
            values[name] = float(number.group(0))
    width, height = values.get("width", 0.0), values.get("height", 0.0)
    if width <= 0 or height <= 0:
        raise PenPlotError(
            status=500,
            code=ErrorCode.PROCESSING_FAILED,
            message="Result has no measurable size.",
        )
    return width, height


def _pixel_size(width: float, height: float, settings: Settings) -> tuple[int, int]:
    """Fit inside the server box without upscaling and keep the aspect."""
    scale = min(
        1.0,
        settings.preview_png_box_w_px / width,
        settings.preview_png_box_h_px / height,
    )
    return max(1, round(width * scale)), max(1, round(height * scale))


def _cache_path(svg: str, settings: Settings, paper: str | None = None) -> tuple[str, str]:
    key = hashlib.sha256(
        (
            svg
            + "\n"
            + _RENDER_VERSION
            + "\n"
            + (paper or "")
            + "\n"
            + raster_out.RENDERER
            + "\n"
            + f"{settings.preview_png_box_w_px}x{settings.preview_png_box_h_px}"
        ).encode("utf-8")
    ).hexdigest()
    directory = os.path.join(settings.data_dir, "result-png")
    return os.path.join(directory, f"{key}.png"), key


def cached_png_path(key: str, settings: Settings) -> str:
    """Path of one content-addressed public PNG (no user-controlled parts)."""
    return os.path.join(settings.data_dir, "result-png", f"{key}.png")


def _read_fresh_png(path: str) -> bytes | None:
    """Bytes of one live cache file; drop it once its 24 h TTL has passed."""
    try:
        age = time.time() - os.stat(path).st_mtime
    except OSError:
        return None
    if age >= PUBLIC_PNG_TTL_SECONDS:
        log.info("result.preview cache expired file=%s", os.path.basename(path))
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def read_cached_png(key: str, settings: Settings) -> bytes | None:
    """Bytes of a cached public PNG, or None when it is absent or expired."""
    return _read_fresh_png(cached_png_path(key, settings))


def _write_atomic(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def public_result_png(
    svg_text: str, settings: Settings, paper: str | None = None
) -> tuple[bytes, str]:
    """PNG bytes and etag for one cached result SVG. Safe off the event loop."""
    path, key = _cache_path(svg_text, settings, paper)
    cached = _read_fresh_png(path)
    if cached is not None:
        return cached, key
    width, height = _dimensions(svg_text)
    width_px, height_px = _pixel_size(width, height, settings)
    started = time.perf_counter()
    data = raster_out.render_png(svg_text, width_px, height_px, paper)
    _write_atomic(path, data)
    log.info(
        "result.preview png_bytes=%d px=%dx%d render_ms=%d renderer=%s",
        len(data),
        width_px,
        height_px,
        int((time.perf_counter() - started) * 1000),
        raster_out.RENDERER,
    )
    return data, key


async def result_response(svg_text: str, settings: Settings) -> Response:
    """SVG on the internal instance, PNG everywhere a browser can reach."""
    if settings.allow_svg:
        return Response(content=svg_text, media_type="image/svg+xml")
    data, etag = await asyncio.to_thread(public_result_png, svg_text, settings)
    return png_response(data, etag)


def png_response(data: bytes, etag: str, extra_headers: dict[str, str] | None = None) -> Response:
    """The only shape a public result route MAY return for a drawing."""
    headers = {
        "ETag": f'"{etag}"',
        "Cache-Control": PUBLIC_CACHE_CONTROL,
        "X-Content-Type-Options": "nosniff",
    }
    if extra_headers:
        headers.update(extra_headers)
    return Response(content=data, media_type="image/png", headers=headers)
