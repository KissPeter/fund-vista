"""Pixel-level helpers: format detection, grayscale load, blur/threshold, SVG parse.

Only permissive-licensed libraries are used here (Pillow HPND, NumPy BSD,
OpenCV Apache-2.0). Deliberately NOT used, per spec §7 licence warning:
Potrace (GPL-2.0) and vpype-flow-imager (GPL-3.0) stay out of the backend so a
future closed-source distribution has no copyleft spill. Contour tracing uses
OpenCV ``findContours``; flow uses our own deterministic field (see methods).

REF-002 Phase 4: raster ops live in :mod:`backend.penplot.raster`, the SVG
vector pipeline in :mod:`backend.penplot.svg_vector`; this module is the
compat shim re-exporting the full public surface, so existing import sites
(routers, pipeline, jobs, tests) keep working unchanged.
"""

from __future__ import annotations

from backend.penplot.raster import (
    EXT_TO_PILLOW_FORMAT,
    a4_dpi,
    adjust_brightness,
    adjust_contrast,
    blur,
    load_raster,
    looks_like_svg,
    maybe_downscale,
    probe_raster,
    remove_background,
    sniff_extension,
    strip_hatch,
    threshold_mask,
)
from backend.penplot.svg_vector import (
    WARNING_EMBEDDED_RASTERS_IGNORED,
    Matrix,
    dist2,
    parse_svg_vectors,
)

__all__ = [
    "EXT_TO_PILLOW_FORMAT",
    "WARNING_EMBEDDED_RASTERS_IGNORED",
    "Matrix",
    "a4_dpi",
    "adjust_brightness",
    "adjust_contrast",
    "blur",
    "dist2",
    "load_raster",
    "looks_like_svg",
    "maybe_downscale",
    "parse_svg_vectors",
    "probe_raster",
    "remove_background",
    "sniff_extension",
    "strip_hatch",
    "threshold_mask",
]
