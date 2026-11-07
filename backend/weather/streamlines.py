"""Evenly spaced streamlines on a lat/lon grid, numpy only.

Jobard-Lefer style: candidate seeds are visited fastest-first, a seed is taken
only if it is at least ``separation`` cells from every accepted point, and each
line is traced forward and backward with a midpoint (RK2) step until it leaves
the box, enters calm air, or comes within ``separation / 2`` of another line.

Work is in grid index space. The wind direction is ``(u / cos(lat), v)`` so
the direction is right on an equirectangular map (the x scale shrinks with
latitude). Output is deterministic: no random numbers, stable sorts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from backend.weather.config import (
    STREAM_MAX_LINE_DEG,
    STREAM_MAX_PATHS,
    STREAM_MAX_TOTAL_KM,
    STREAM_MIN_SPEED_MS,
    STREAM_SEPARATION_CELLS,
)
from backend.weather.geo import polyline_length_km


@dataclass(frozen=True)
class Streamline:
    lon: np.ndarray
    lat: np.ndarray
    mean_speed: float  # m/s, mean along the line
    length_km: float


def _grid_directions(u: np.ndarray, v: np.ndarray, lat: np.ndarray, lon: np.ndarray):
    """Unit direction per cell in index space, plus speed (m/s)."""
    dlon = float(lon[1] - lon[0])
    dlat = float(lat[1] - lat[0])
    cos_lat = np.cos(np.radians(lat))[:, None]
    with np.errstate(invalid="ignore", divide="ignore"):
        dx = (u / cos_lat) / dlon
        dy = v / dlat
        mag = np.hypot(dx, dy)
        ux = dx / mag
        uy = dy / mag
    speed = np.hypot(u, v)
    return ux, uy, speed


def _bilinear(field: list, x: float, y: float) -> float:
    """Bilinear sample of a nested list ``field[row][col]`` at index (x, y)."""
    ny = len(field)
    nx = len(field[0])
    x0 = int(math.floor(x))
    y0 = int(math.floor(y))
    if x0 < 0 or y0 < 0 or x0 >= nx - 1 or y0 >= ny - 1:
        if x == nx - 1 and 0 <= y <= ny - 1:
            x0, x = nx - 2, nx - 1.0
        elif y == ny - 1 and 0 <= x <= nx - 1:
            y0, y = ny - 2, ny - 1.0
        else:
            return math.nan
    fx = x - x0
    fy = y - y0
    row0 = field[y0]
    row1 = field[y0 + 1]
    a = row0[x0]
    b = row0[x0 + 1]
    c = row1[x0]
    d = row1[x0 + 1]
    return float((a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy)


class _Occupancy:
    """Spatial hash of accepted points. Bins are no larger than the test
    distance, so a query touches only a few points."""

    def __init__(self, bin_size: float) -> None:
        self.bin = bin_size
        self.cells: dict[tuple[int, int], list[tuple[float, float]]] = {}

    def add(self, x: float, y: float) -> None:
        key = (int(math.floor(x / self.bin)), int(math.floor(y / self.bin)))
        self.cells.setdefault(key, []).append((x, y))

    def closer_than(self, x: float, y: float, dist: float) -> bool:
        cx = int(math.floor(x / self.bin))
        cy = int(math.floor(y / self.bin))
        r = int(math.ceil(dist / self.bin))
        d2 = dist * dist
        for i in range(cx - r, cx + r + 1):
            for j in range(cy - r, cy + r + 1):
                for px, py in self.cells.get((i, j), ()):
                    if (px - x) ** 2 + (py - y) ** 2 < d2:
                        return True
        return False


def streamlines(
    u: np.ndarray,
    v: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    *,
    separation: float = STREAM_SEPARATION_CELLS,
    max_paths: int = STREAM_MAX_PATHS,
    max_total_km: float = STREAM_MAX_TOTAL_KM,
    min_speed: float = STREAM_MIN_SPEED_MS,
    step: float = 0.5,
    max_line_deg: float = STREAM_MAX_LINE_DEG,
    min_length_cells: float = 3.0,
) -> list[Streamline]:
    """Evenly spaced streamlines, fastest first, within the line budget.

    ``max_paths`` and ``max_total_km`` are hard budgets: once either is
    reached no further line is started. ``lat`` and ``lon`` must both be
    ascending and uniformly spaced; the index step is taken per axis.
    """
    ux_a, uy_a, speed_a = _grid_directions(np.asarray(u, float), np.asarray(v, float), lat, lon)
    ny, nx = ux_a.shape
    # Plain nested lists: per-cell reads in the tracer are ~10x faster than
    # numpy scalar indexing, and the tracer does millions of them.
    ux = ux_a.tolist()
    uy = uy_a.tolist()
    speed = speed_a.tolist()
    dlon = float(lon[1] - lon[0])
    dlat = float(lat[1] - lat[0])
    d_sep = float(separation)
    d_test = 0.5 * d_sep
    occ = _Occupancy(bin_size=d_test)
    # Seeds already tried (accepted or not): their neighbours are not retried.
    tried = _Occupancy(bin_size=d_test)

    # Seeds: a lattice at half a separation, fastest first (stable ties).
    seed_step = max(d_sep / 2.0, 0.5)
    ys = np.arange(0.0, ny - 1 + 1e-9, seed_step)
    xs = np.arange(0.0, nx - 1 + 1e-9, seed_step)
    gy, gx = np.meshgrid(ys, xs, indexing="ij")
    gx = gx.ravel()
    gy = gy.ravel()
    seed_speed = np.array([_bilinear(speed, x, y) for x, y in zip(gx, gy)])
    keep = np.isfinite(seed_speed) & (seed_speed >= max(min_speed, 1e-9))
    order = np.argsort(-seed_speed[keep], kind="stable")
    cand = list(zip(gx[keep][order], gy[keep][order]))

    def sample_dir(x: float, y: float):
        cx = _bilinear(ux, x, y)
        cy = _bilinear(uy, x, y)
        if not (math.isfinite(cx) and math.isfinite(cy)):
            return None
        m = math.hypot(cx, cy)
        if m < 1e-12:
            return None
        return cx / m, cy / m

    def trace(x0: float, y0: float, sign: float) -> list[tuple[float, float]]:
        pts: list[tuple[float, float]] = []
        x, y = x0, y0
        for _ in range(max_steps):
            k1 = sample_dir(x, y)
            if k1 is None:
                break
            xm = x + sign * 0.5 * step * k1[0]
            ym = y + sign * 0.5 * step * k1[1]
            k2 = sample_dir(xm, ym)
            if k2 is None:
                break
            nxp = x + sign * step * k2[0]
            nyp = y + sign * step * k2[1]
            if not (0.0 <= nxp <= nx - 1 and 0.0 <= nyp <= ny - 1):
                break
            spd = _bilinear(speed, nxp, nyp)
            if not math.isfinite(spd) or spd < min_speed:
                break
            if occ.closer_than(nxp, nyp, d_test):
                break
            pts.append((nxp, nyp))
            x, y = nxp, nyp
        return pts

    # Each line is capped at ``max_line_deg`` of total length, split evenly
    # between the forward and backward traces (plot-time budget per path).
    max_steps = max(1, int(math.ceil(max_line_deg / dlon / 2.0 / step)))

    lines: list[Streamline] = []
    total_km = 0.0
    for x0, y0 in cand:
        if len(lines) >= max_paths or total_km >= max_total_km:
            break
        if occ.closer_than(x0, y0, d_sep) or tried.closer_than(x0, y0, d_sep):
            continue
        tried.add(x0, y0)
        back = trace(x0, y0, -1.0)
        fwd = trace(x0, y0, 1.0)
        path = list(reversed(back)) + [(x0, y0)] + fwd
        arr = np.asarray(path, dtype=float)
        run = float(np.sum(np.hypot(np.diff(arr[:, 0]), np.diff(arr[:, 1]))))
        if run < min_length_cells:
            continue
        lon_pts = lon[0] + arr[:, 0] * dlon
        lat_pts = lat[0] + arr[:, 1] * dlat
        # A line that would overrun the paper budget is skipped.
        km = polyline_length_km(lon_pts, lat_pts)
        if total_km + km > max_total_km:
            continue
        mean_speed = float(np.mean([_bilinear(speed, px, py) for px, py in arr[:: max(1, len(arr) // 12)]]))
        for px, py in path:
            occ.add(px, py)
        total_km += km
        lines.append(Streamline(lon=lon_pts.astype(np.float32), lat=lat_pts.astype(np.float32),
                                mean_speed=mean_speed, length_km=km))
    return lines


def total_length_km(lines: list[Streamline]) -> float:
    return float(sum(line.length_km for line in lines))
