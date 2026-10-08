"""Line-generation methods behind a single protocol.

``LineMethod.generate(mask, gray, ctx) -> list[Polyline]`` where a Polyline is
an ordered list of (x, y) points in *source pixel space*. The pipeline scales
to millimetres later (optimize.layout), so methods stay resolution-local and
unit-testable without page math.

Licence note (spec §7): Potrace is GPL-2.0-or-later and vpype-flow-imager is
GPL-3.0. Both are intentionally NOT dependencies here — contour tracing uses
OpenCV (Apache-2.0), centerline tracing uses a NumPy-only Zhang-Suen thinning
pass, curve smoothing is Chaikin corner-cutting in pure Python, and flow uses
a small built-in deterministic field, so the backend stays permissively
licensed (Pillow HPND / NumPy BSD / OpenCV Apache).
If a future v2 wants Potrace fidelity, add a ``PotraceMethod`` class behind
this same protocol and gate it behind an optional extra.

REF-002 Phase 4: one class per module (``contour`` / ``hatch`` / ``flow`` /
``centerline`` + its skeleton machinery); this module keeps the protocol,
the context, the registry, and the re-exports so existing import sites
keep working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from backend.penplot.centerline import CenterlineMethod
from backend.penplot.contour import ContourMethod
from backend.penplot.flow import FlowMethod
from backend.penplot.hatch import HatchMethod

Polyline = list[tuple[float, float]]


@dataclass(frozen=True)
class MethodContext:
    threshold: int
    blur_radius: float
    hatch_pitch_mm: float
    contour_simplify: float
    hatch_angle_deg: float = 45.0
    # Hatch pitch already converted to pixels by the pipeline (page-aware).
    hatch_pitch_px: float = 8.0
    # Centerline (skeleton) tracing: drop skeleton branches shorter than this
    # many pixels (thinning spurs from stroke edges/noise). 0 disables pruning.
    centerline_prune_px: int = 4


class LineMethod(Protocol):
    name: str

    def generate(
        self, mask: np.ndarray, gray: np.ndarray, ctx: MethodContext
    ) -> list[Polyline]:
        ...


METHOD_REGISTRY: dict[str, LineMethod] = {
    "contour": ContourMethod(),
    "centerline": CenterlineMethod(),
    "hatch": HatchMethod(),
    "flow": FlowMethod(),
}


__all__ = [
    "METHOD_REGISTRY",
    "CenterlineMethod",
    "ContourMethod",
    "FlowMethod",
    "HatchMethod",
    "LineMethod",
    "MethodContext",
    "Polyline",
]
