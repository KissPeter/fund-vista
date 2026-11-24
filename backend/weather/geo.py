"""Small geodesy helpers on a lat/lon grid (equirectangular, spherical)."""

from __future__ import annotations

import math

import numpy as np

from backend.traffic.geo import rdp
from backend.weather.config import KM_PER_DEG_LAT


def polyline_length_km(lon: np.ndarray, lat: np.ndarray) -> float:
    """Length of a lon/lat polyline in km, each segment at its mean latitude."""
    if len(lon) < 2:
        return 0.0
    dlon = np.diff(lon)
    dlat = np.diff(lat)
    mid = np.radians((lat[:-1] + lat[1:]) / 2.0)
    dx = dlon * KM_PER_DEG_LAT * np.cos(mid)
    dy = dlat * KM_PER_DEG_LAT
    return float(np.sum(np.hypot(dx, dy)))


def ring_area_km2(lon: np.ndarray, lat: np.ndarray) -> float:
    """Absolute area of a closed lon/lat ring (shoelace on a local flat map)."""
    lat0 = math.radians(float(np.mean(lat)))
    x = lon * KM_PER_DEG_LAT * math.cos(lat0)
    y = lat * KM_PER_DEG_LAT
    return abs(float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))) / 2.0


def km_between(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Straight-line distance in km on the local flat approximation."""
    mid = math.radians((lat1 + lat2) / 2.0)
    dx = (lon2 - lon1) * KM_PER_DEG_LAT * math.cos(mid)
    dy = (lat2 - lat1) * KM_PER_DEG_LAT
    return math.hypot(dx, dy)


def simplify(pts: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    """Ramer-Douglas-Peucker that also works on closed rings.

    ``backend.traffic.geo.rdp`` measures distance to the chord from the first to
    the last point; on a closed ring that chord is a point and every distance is
    zero, so the ring collapses to its two end points. A closed ring is split at
    its point farthest from the start first, then each half is simplified.
    """
    if len(pts) >= 4 and tuple(pts[0]) == tuple(pts[-1]):
        x0, y0 = pts[0]
        far = max(range(1, len(pts) - 1), key=lambda k: math.hypot(pts[k][0] - x0, pts[k][1] - y0))
        first = rdp(list(pts[: far + 1]), eps)
        second = rdp(list(pts[far:]), eps)
        return first + second[1:]
    return rdp(list(pts), eps)
