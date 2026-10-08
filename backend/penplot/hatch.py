"""Tonal hatching method (REF-002 Phase 4).

Pure move from :mod:`backend.penplot.methods` — identical behavior.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from backend.penplot.methods import MethodContext, Polyline

log = logging.getLogger(__name__)


class HatchMethod:
    """Tonal hatching at ``hatch_angle_deg`` clipped to the ink mask.

    For each hatch line offset by ``hatch_pitch_px`` along the line normal, the
    intersections with dark runs are emitted as segments. Very dark regions
    get a second cross pass at +90° for tone depth — classic pen-plot shading.
    """

    name = "hatch"

    def generate(
        self, mask: np.ndarray, gray: np.ndarray, ctx: MethodContext
    ) -> list[Polyline]:
        h, w = mask.shape[:2]
        pitch = max(2.0, float(ctx.hatch_pitch_px))
        angle = math.radians(float(ctx.hatch_angle_deg) % 180.0)
        cross_angle = angle + math.radians(90.0)
        main = self._hatch_at_angle(mask, w, h, pitch, angle)
        # Cross-hatch only the darkest quartile for depth; cheap and effective.
        dark = gray < max(0, ctx.threshold - 64)
        cross: list[Polyline] = []
        if bool(np.any(dark)):
            cross = self._hatch_at_angle(dark, w, h, pitch * 2.0, cross_angle)
        log.debug("hatch: %d + %d cross segments", len(main), len(cross))
        return main + cross

    @staticmethod
    def _hatch_at_angle(
        mask: np.ndarray, w: int, h: int, pitch: float, angle: float
    ) -> list[Polyline]:
        dx, dy = math.cos(angle), math.sin(angle)
        nx, ny = -dy, dx  # line normal
        # Project corners onto the normal to bound the offset range.
        corners = [(0.0, 0.0), (w, 0.0), (0.0, h), (w, h)]
        projs = [x * nx + y * ny for x, y in corners]
        lo, hi = min(projs), max(projs)
        out: list[Polyline] = []
        # March along each hatch line in 1px steps, collecting dark runs.
        diag = math.hypot(w, h)
        steps = int(diag) + 1
        offset = lo
        while offset <= hi:
            # A point on the line: normal*offset shifted to bbox centre.
            bx, by = nx * offset, ny * offset
            run: Polyline = []
            for s in range(-steps, steps + 1):
                x = bx + dx * s
                y = by + dy * s
                ix, iy = int(round(x)), int(round(y))
                inside = 0 <= ix < w and 0 <= iy < h
                dark = bool(mask[iy, ix]) if inside else False
                if dark:
                    run.append((x, y))
                else:
                    if len(run) >= 2:
                        out.append(run[::2] if len(run) > 64 else run)
                    run = []
            if len(run) >= 2:
                out.append(run[::2] if len(run) > 64 else run)
            offset += pitch
        return out


__all__ = ["HatchMethod"]
