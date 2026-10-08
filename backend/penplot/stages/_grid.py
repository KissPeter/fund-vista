"""Shared stage primitives (REF-002 Phase 4).

Pure moves from :mod:`backend.penplot.optimize` — identical behavior.
Grid hashing (the twice-duplicated ``cell()`` closures, parameterized
into one :func:`grid_cell`) plus the measurement helpers every stage
logs with.
"""

from __future__ import annotations

import logging
import math

import numpy as np

from backend.penplot.methods import Polyline

log = logging.getLogger(__name__)


def grid_cell(p: tuple[float, float], size: float) -> tuple[int, int]:
    """Spatial-hash cell for ``p`` at cell size ``size``.

    Cell size = tolerance: points close enough to join (distance <= tol)
    are always within 2 cells of each other, so a 5x5 neighborhood is
    safe; the exact distance test at the call site filters approximate
    from true matches.
    """
    return (math.floor(p[0] / size), math.floor(p[1] / size))


def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def polyline_length(pl: Polyline) -> float:
    xs = np.asarray(pl, dtype=float)
    return float(
        np.hypot(xs[1:, 0] - xs[:-1, 0], xs[1:, 1] - xs[:-1, 1]).sum()
    )


def count_points(lines: list[Polyline]) -> int:
    return sum(len(pl) for pl in lines)


__all__ = ["count_points", "dist", "grid_cell", "polyline_length"]
