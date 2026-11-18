"""Contour lines for MSLP, Z500 and T500 on a Gaussian-smoothed grid.

``contourpy`` (BSD-3-Clause) does the marching; smoothing is a separable
Gaussian in numpy so no scipy is needed. Lines that are too short, and closed
rings that enclose too little area, are dropped: they plot as specks.
"""

from __future__ import annotations

from dataclasses import dataclass

import contourpy
import numpy as np

from backend.weather.config import MIN_CLOSED_AREA_KM2, MIN_CONTOUR_LENGTH_KM, SMOOTH_SIGMA_CELLS
from backend.weather.geo import polyline_length_km, ring_area_km2


@dataclass(frozen=True)
class ContourLine:
    level: float
    lon: np.ndarray
    lat: np.ndarray
    closed: bool
    length_km: float
    heavy: bool


def gaussian_smooth(field: np.ndarray, sigma: float = SMOOTH_SIGMA_CELLS) -> np.ndarray:
    """Separable Gaussian blur, edge-padded. ``sigma <= 0`` returns a float copy."""
    data = np.asarray(field, dtype=float)
    if sigma <= 0:
        return data.copy()
    radius = int(np.ceil(3.0 * sigma))
    x = np.arange(-radius, radius + 1, dtype=float)
    kernel = np.exp(-0.5 * (x / sigma) ** 2)
    kernel /= kernel.sum()
    padded = np.pad(data, radius, mode="edge")
    ny, nx = data.shape
    rows = np.zeros((padded.shape[0], nx))
    for i, w in enumerate(kernel):
        rows += w * padded[:, i:i + nx]
    out = np.zeros((ny, nx))
    for i, w in enumerate(kernel):
        out += w * rows[i:i + ny, :]
    return out


def levels_between(field: np.ndarray, step: float) -> np.ndarray:
    """Multiples of ``step`` covering the finite range of ``field``."""
    finite = field[np.isfinite(field)]
    if finite.size == 0:
        return np.array([], dtype=float)
    lo = np.floor(finite.min() / step) * step
    hi = finite.max() + step
    return np.arange(lo, hi, step)


def contour_lines(
    field: np.ndarray,
    lon: np.ndarray,
    lat: np.ndarray,
    levels: np.ndarray,
    *,
    heavy_every: int | None = None,
    sigma: float = SMOOTH_SIGMA_CELLS,
    min_length_km: float = MIN_CONTOUR_LENGTH_KM,
    min_area_km2: float = MIN_CLOSED_AREA_KM2,
) -> list[ContourLine]:
    """Contour lines at ``levels``, ordered by level then by generator order.

    ``heavy_every=n`` marks every n-th level (by its index in the step grid)
    as heavy. Deterministic for a given input.
    """
    z = gaussian_smooth(field, sigma)
    gen = contourpy.contour_generator(
        lon, lat, z, name="serial", line_type=contourpy.LineType.Separate
    )
    step = float(levels[1] - levels[0]) if len(levels) > 1 else 1.0
    out: list[ContourLine] = []
    for level in levels:
        heavy = bool(heavy_every) and int(round(level / step)) % heavy_every == 0
        for pts in gen.lines(float(level)):
            if len(pts) < 3:
                continue
            p_lon = np.asarray(pts[:, 0], dtype=float)
            p_lat = np.asarray(pts[:, 1], dtype=float)
            closed = bool(np.allclose(pts[0], pts[-1]))
            length = polyline_length_km(p_lon, p_lat)
            if length < min_length_km:
                continue
            if closed and ring_area_km2(p_lon[:-1], p_lat[:-1]) < min_area_km2:
                continue
            out.append(ContourLine(float(level), p_lon, p_lat, closed, length, heavy))
    return out
