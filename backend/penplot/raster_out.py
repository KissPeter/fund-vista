"""Gallery preview PNG: composed sheet SVG -> PNG bytes at a server-fixed size.

Used only by ``POST /v1/compose-sheet`` with ``preview="png"``. The SVG has
already been composed and checked by ``compose_sheet``; this module only
rasterizes it.

Renderer: ``resvg`` through the ``resvg-py`` binding (binding MIT, resvg itself
MPL-2.0; see ``backend/THIRD_PARTY_NOTICES.md`` section B). No fallback
renderer is used: if the declared wheel is missing, rasterization fails closed.

Safety: the SVG is parsed with ``defusedxml`` before rendering, resvg gets no
``resources_dir`` (no external ``href`` is loaded), and system fonts are not
scanned (``skip_system_fonts``). Nothing is read from or written to the file
system here, and nothing is fetched from the network.

Memory: the RGBA pixmap is ``width * height * 4`` bytes (about 33 MB at the
2160 x 1528 default, about 13 MB). The PNG encoder keeps a second copy while it
runs, so one render peaks near 2x that. Measured numbers are in the PR and in
``docs/F-007-gallery-sheet-compose.md``.
"""

from __future__ import annotations

import logging
import re

from defusedxml import ElementTree as DefusedET

from backend.penplot.errors import ErrorCode, PenPlotError

log = logging.getLogger(__name__)

RENDERER = "resvg-py"


def preview_size(width_mm: float, height_mm: float, box_w_px: int, box_h_px: int) -> tuple[int, int]:
    """Pixel size of the sheet fitted inside the box, aspect kept, never enlarged.

    The scale is the larger of the two fits that stays inside the box, so one
    edge of the sheet touches the box.
    """
    if width_mm <= 0 or height_mm <= 0:
        raise ValueError("sheet size must be positive")
    scale = min(box_w_px / width_mm, box_h_px / height_mm)
    return max(1, round(width_mm * scale)), max(1, round(height_mm * scale))


_ROOT_TAG_RE = re.compile(r"<svg\b[^>]*>")
_ROOT_WIDTH_RE = re.compile(r'\swidth="[^"]*"')
_ROOT_HEIGHT_RE = re.compile(r'\sheight="[^"]*"')


def _pixel_root(svg: str, width_px: int, height_px: int) -> str:
    """The sheet with its root ``width``/``height`` set to pixels.

    The composed sheet declares its size in mm (``210mm``); resvg-py rejects
    physical units in ``width``/``height``. Only the root tag changes, and the
    viewBox keeps the drawing geometry.
    """
    root = _ROOT_TAG_RE.search(svg)
    if root is None:
        raise ValueError("no root svg element")
    tag = _ROOT_WIDTH_RE.sub(f' width="{width_px}"', root.group(0), count=1)
    tag = _ROOT_HEIGHT_RE.sub(f' height="{height_px}"', tag, count=1)
    return svg[: root.start()] + tag + svg[root.end():]


def render_png(svg: str, width_px: int, height_px: int, background: str | None) -> bytes:
    """Rasterize ``svg`` to a PNG of exactly ``width_px`` x ``height_px``.

    ``background`` is a CSS colour for the canvas (the paper). ``None`` leaves it
    transparent. Raises :class:`PenPlotError` (500, processing_failed) when the
    renderer fails on a sheet the compose step already accepted.
    """
    try:
        DefusedET.fromstring(svg.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - any parse failure is a server fault here
        raise PenPlotError(
            status=500, code=ErrorCode.PROCESSING_FAILED,
            message="Composed sheet is not a well-formed SVG.",
        ) from exc
    try:
        pixel_svg = _pixel_root(svg, width_px, height_px)
    except ValueError as exc:
        raise PenPlotError(
            status=500, code=ErrorCode.PROCESSING_FAILED,
            message="Composed sheet has no root element.",
        ) from exc
    try:
        return _render_resvg(pixel_svg, width_px, height_px, background)
    except ValueError as exc:
        log.warning("preview.render failed renderer=%s", RENDERER)
        raise PenPlotError(
            status=500, code=ErrorCode.PROCESSING_FAILED,
            message="Preview rendering failed.",
        ) from exc


def _render_resvg(svg: str, width_px: int, height_px: int, background: str | None) -> bytes:
    import resvg_py  # noqa: PLC0415 - optional at import time, required at render time

    return bytes(
        resvg_py.svg_to_bytes(
            svg_string=svg,
            width=width_px,
            height=height_px,
            background=background,
            skip_system_fonts=True,
        )
    )

