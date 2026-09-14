"""Local-plane projection + single-rotation alignment (pure functions).

Pipeline order (spec steps 4–5): project lon/lat to flat meters about the
airport center, compute ONE rotation from the primary (longest, open)
runway, apply that same matrix to every feature class. The compass arrow is
never left pointing up — north is rotated by the same amount, so the drawn
arrow is ``true_north − applied_rotation`` by construction.
"""

from __future__ import annotations

import math

_M_PER_DEG_LAT = 110540.0
_M_PER_DEG_LON_EQUATOR = 111320.0


def project(
    lon: float, lat: float, lon0: float, lat0: float
) -> tuple[float, float]:
    """Equirectangular meters about (lon0, lat0); +x east, +y north."""
    x = (lon - lon0) * _M_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat0))
    y = (lat - lat0) * _M_PER_DEG_LAT
    return x, y


def runway_heading_deg(
    le_lonlat: tuple[float, float], he_lonlat: tuple[float, float]
) -> float:
    """True heading LE→HE in degrees clockwise from north (0–360)."""
    dx = he_lonlat[0] - le_lonlat[0]
    dy = he_lonlat[1] - le_lonlat[1]
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def rotation_for_heading(heading_deg: float) -> float:
    """CCW rotation (degrees) that puts a runway line vertical, minimal turn.

    A runway is a line (070/250 are the same strip), so the rotation is
    normalized to (−90, 90]: ``((h + 90) % 180) − 90``. Positive = CCW in
    the (east, north) plane.
    """
    return ((heading_deg + 90.0) % 180.0) - 90.0


def rotate_point(x: float, y: float, rotation_deg_ccw: float) -> tuple[float, float]:
    """Rotate one plane point CCW by ``rotation_deg_ccw`` degrees."""
    rad = math.radians(rotation_deg_ccw)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    return (x * cos_r - y * sin_r, x * sin_r + y * cos_r)


def rotate_all(
    polys: list[list[tuple[float, float]]], rotation_deg_ccw: float
) -> list[list[tuple[float, float]]]:
    """Apply the single scene rotation to a list of polylines."""
    return [
        [rotate_point(x, y, rotation_deg_ccw) for x, y in ring] for ring in polys
    ]


def primary_runway_index(lengths_ft: list[float | None]) -> int:
    """Index of the longest runway (unknown lengths sort as 0)."""
    best, best_len = 0, -1.0
    for i, length in enumerate(lengths_ft):
        value = float(length or 0.0)
        if value > best_len:
            best, best_len = i, value
    return best


__all__ = [
    "primary_runway_index",
    "project",
    "rotate_all",
    "rotate_point",
    "rotation_for_heading",
    "runway_heading_deg",
]
