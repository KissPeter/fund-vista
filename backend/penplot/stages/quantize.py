"""Input quantization stage (REF-002 Phase 4).

Pure move from :mod:`backend.penplot.optimize` — identical behavior.
vpype ``read --quantization`` equivalent: runs right after layout so all
downstream tolerances operate on quantized input.
"""

from __future__ import annotations

import logging

from backend.penplot.methods import Polyline
from backend.penplot.stages._grid import count_points, dist

log = logging.getLogger(__name__)


def quantize(lines: list[Polyline], step: float) -> list[Polyline]:
    """Snap every coordinate to a ``step`` grid (mm), dropping degenerate lines.

    Runs right after layout so — like vpype — all downstream tolerances operate
    on quantized input. Consecutive duplicates created by snapping are removed;
    polylines collapsing below 2 points are dropped (stats stay consistent).
    """
    if step <= 0:
        return [list(pl) for pl in lines]
    out: list[Polyline] = []
    for pl in lines:
        snapped = [(round(x / step) * step, round(y / step) * step) for x, y in pl]
        deduped = [snapped[0]]
        for pt in snapped[1:]:
            if dist(pt, deduped[-1]) > 1e-12:
                deduped.append(pt)
        if len(deduped) >= 2:
            out.append(deduped)
    log.debug(
        "quantize: %d -> %d pts (step=%.3fmm)", count_points(lines), count_points(out), step
    )
    return out


__all__ = ["quantize"]
