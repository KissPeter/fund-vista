"""vpype-equivalent cleanup in pure Python (MIT-safe, no binary dep).

Stage order mirrors the Drawscape/vpype chain from spec §3.3 so the returned
``vpype_command`` string stays an honest description of what ran::

    layout -> quantize (= read --quantization) -> linemerge -> linesimplify
        -> linesort -> reloop -> write

All geometry here is in millimetres *after* layout, except linemerge/simplify
tolerances which arrive in mm and are applied post-layout (same as vpype,
which works in final units). Points/segments before/after are counted around
simplify+merge so ``stats`` reflects real savings.

REF-002 Phase 4: the stages live in :mod:`backend.penplot.stages`
(``merge`` / ``simplify`` / ``sort`` / ``layout_svg`` / ``quantize`` +
shared :mod:`backend.penplot.stages._grid`); this module is the compat
shim re-exporting the full public surface, so existing import sites
(``pipeline``, tests) keep working unchanged.
"""

from __future__ import annotations

from backend.penplot.stages._grid import count_points, dist, polyline_length
from backend.penplot.stages.layout_svg import (
    build_vpype_command,
    layout,
    layout_scale,
    page_dims_mm,
    to_svg,
)
from backend.penplot.stages.merge import linemerge
from backend.penplot.stages.quantize import quantize
from backend.penplot.stages.simplify import curvesmooth, densify, linesimplify
from backend.penplot.stages.sort import linesort, reloop

__all__ = [
    "build_vpype_command",
    "count_points",
    "curvesmooth",
    "densify",
    "dist",
    "layout",
    "layout_scale",
    "linemerge",
    "linesimplify",
    "linesort",
    "page_dims_mm",
    "polyline_length",
    "quantize",
    "reloop",
    "to_svg",
]
