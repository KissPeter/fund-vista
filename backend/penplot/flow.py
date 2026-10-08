"""Flow-field streamline method (REF-002 Phase 4).

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


class FlowMethod:
    """Deterministic organic streamlines modulated by image tone.

    Honoured params (C.2.4): only ``threshold``/``blur_radius``/``contrast``
    (via the mask the pipeline builds) and the image itself are used.
    ``hatch_pitch_mm`` and ``contour_simplify`` are IGNORED by design — the
    field is a fixed-coarse deterministic advection, not line spacing.
    Particles seed on a coarse grid and advect through a cheap analytic field
    (layered sin/cos — no RNG at runtime beyond a fixed seed ordering), dying
    in bright areas. Darker pixels let lines grow longer, so tone emerges from
    line density. Bounded: seeds × max steps are capped so a pathological
    image can't blow up convert latency.
    """

    name = "flow"
    seed_step_px = 28
    max_steps = 220
    step_px = 4.0

    def generate(
        self, mask: np.ndarray, gray: np.ndarray, ctx: MethodContext
    ) -> list[Polyline]:
        h, w = gray.shape[:2]
        tone = (255.0 - gray.astype(np.float32)) / 255.0  # 0 bright .. 1 black
        out: list[Polyline] = []
        for gy in range(self.seed_step_px // 2, h, self.seed_step_px):
            for gx in range(self.seed_step_px // 2, w, self.seed_step_px):
                if tone[gy, gx] < 0.08:
                    continue  # skip paper-white seeds
                line = self._trace(gx, gy, tone, w, h)
                if len(line) >= 4:
                    out.append(line[::2])
                if len(out) >= 600:  # CPU guard
                    log.debug("flow: hit 600-line cap")
                    return out
        log.debug("flow: %d streamlines", len(out))
        return out

    def _trace(
        self, x0: float, y0: float, tone: np.ndarray, w: int, h: int
    ) -> Polyline:
        x, y = float(x0), float(y0)
        line: Polyline = [(x, y)]
        bright_run = 0
        for _ in range(self.max_steps):
            # Analytic pseudo-turbulent field; fully deterministic.
            a = (
                1.6 * math.sin(0.012 * x + 1.7)
                + 1.2 * math.cos(0.015 * y - 0.6)
                + 0.8 * math.sin(0.006 * (x + y))
            )
            x += math.cos(a) * self.step_px
            y += math.sin(a) * self.step_px
            if not (0 <= x < w and 0 <= y < h):
                break
            t = float(tone[int(y), int(x)])
            if t < 0.05:
                bright_run += 1
                if bright_run > 12:
                    break
            else:
                bright_run = 0
            line.append((x, y))
        return line


__all__ = ["FlowMethod"]
