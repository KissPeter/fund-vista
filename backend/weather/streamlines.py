"""Evenly spaced streamlines in sheet millimetres, numpy only.

Jobard-Lefer style: seeds are scanned over the whole view in a fixed raster
order (never sorted by speed), a seed is taken only if it lies at least
``separation_mm`` from every accepted line point, and each seed grows a line
forward and backward (RK2 midpoint steps) until it leaves the view, meets an
existing line closer than ``separation_mm / 2``, or enters calm air. The two
halves are stitched into one polyline. Lines shorter than ``min_length_mm``
are dropped.

The separation sets the density, so the budget (paths and total paper length)
is met by thinning, not by truncating: when the result is over budget the
separation grows and the whole pass is repeated, at most ``tries`` times. A
thinned result carries ``thinned=True`` (the renderer warns
``streamlines_thinned``).

Work is in paper millimetres (x right, y down, the :class:`Frame` of
``backend.weather.render``). The wind direction on the paper is
``(u * kx / cos(lat), -v)``: the x scale shrinks with latitude, north is up.
Output is deterministic: no random numbers, a fixed scan order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from backend.weather.config import (
    STREAM_MAX_PATHS,
    STREAM_MAX_TRIES,
    STREAM_MIN_LENGTH_MM,
    STREAM_MIN_SPEED_MS,
    STREAM_PAPER_BUDGET_MM,
    STREAM_SEPARATION_MM,
)


@dataclass(frozen=True)
class Streamline:
    """One line in sheet millimetres (the same frame as the artwork)."""

    x: np.ndarray
    y: np.ndarray
    length_mm: float


@dataclass(frozen=True)
class StreamResult:
    lines: list[Streamline] = field(default_factory=list)
    separation_mm: float = STREAM_SEPARATION_MM
    thinned: bool = False
    over_budget: bool = False


def _direction_cells(u: np.ndarray, v: np.ndarray, lat: np.ndarray, kx: float):
    """Unit paper direction per grid cell, plus the speed (m/s)."""
    cos_lat = np.cos(np.radians(lat))[:, None]
    dx = u * kx / cos_lat
    dy = -v
    with np.errstate(invalid="ignore", divide="ignore"):
        mag = np.hypot(dx, dy)
        ux = dx / mag
        uy = dy / mag
    return ux, uy, np.hypot(u, v)


def _bilinear(rows: list, x: float, y: float) -> float:
    """Bilinear sample of a nested list ``rows[iy][ix]`` at fractional index (x, y)."""
    ny = len(rows)
    nx = len(rows[0])
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
    r0 = rows[y0]
    r1 = rows[y0 + 1]
    return float((r0[x0] * (1 - fx) + r0[x0 + 1] * fx) * (1 - fy)
                 + (r1[x0] * (1 - fx) + r1[x0 + 1] * fx) * fy)


class _Occupancy:
    """Spatial hash of accepted points; bins no larger than the test distance."""

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


def _self_hit(own: dict, pts: list, x: float, y: float, dist: float, bin_size: float,
              recent_gap: int) -> bool:
    """True when (x, y) lies within ``dist`` of this trace's own older points."""
    n = len(pts)
    cx = int(math.floor(x / bin_size))
    cy = int(math.floor(y / bin_size))
    r = int(math.ceil(dist / bin_size))
    d2 = dist * dist
    for i in range(cx - r, cx + r + 1):
        for j in range(cy - r, cy + r + 1):
            for k in own.get((i, j), ()):
                if k >= n - recent_gap:
                    continue
                px, py = pts[k]
                if (px - x) ** 2 + (py - y) ** 2 < d2:
                    return True
    return False


def _polyline_mm(pts: list[tuple[float, float]]) -> float:
    return float(sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])))


def _one_pass(ux, uy, speed, lon_axis, lat_axis, frame, separation_mm: float,
              min_speed: float, step_mm: float, min_length_mm: float) -> list[Streamline]:
    """One full seeding pass at a fixed separation."""
    box = frame.box
    lon0 = float(frame.lon0)
    lat1 = float(frame.lat1)
    kx_scale = float(frame.kx) * float(frame.scale)
    scale = float(frame.scale)
    off_x = float(frame.off_x)
    off_y = float(frame.off_y)
    lon_start = float(lon_axis[0])
    lat_start = float(lat_axis[0])
    dlon = float(lon_axis[1] - lon_axis[0])
    dlat = float(lat_axis[1] - lat_axis[0])

    def to_index(x: float, y: float) -> tuple[float, float]:
        lon = lon0 + (x - off_x) / kx_scale
        lat = lat1 - (y - off_y) / scale
        return (lon - lon_start) / dlon, (lat - lat_start) / dlat

    def direction(x: float, y: float):
        ix, iy = to_index(x, y)
        cx = _bilinear(ux, ix, iy)
        cy = _bilinear(uy, ix, iy)
        if not (math.isfinite(cx) and math.isfinite(cy)):
            return None
        m = math.hypot(cx, cy)
        if m < 1e-12:
            return None
        if min_speed > 0.0 and _bilinear(speed, ix, iy) < min_speed:
            return None
        return cx / m, cy / m

    def inside(x: float, y: float) -> bool:
        return box.x0 <= x <= box.x1 and box.y0 <= y <= box.y1

    d_test = 0.5 * separation_mm
    occ = _Occupancy(bin_size=d_test)
    max_steps = 4000

    # A trace that circles back onto its own earlier path (a closed orbit round
    # a pressure centre) stops; points less than this many steps back are
    # its own recent path and do not count.
    recent_gap = int(math.ceil(3.0 * separation_mm / step_mm)) + 1
    bin_own = d_test

    def trace(x0: float, y0: float, sign: float) -> list[tuple[float, float]]:
        pts: list[tuple[float, float]] = []
        own: dict[tuple[int, int], list[int]] = {}
        x, y = x0, y0
        for _ in range(max_steps):
            d1 = direction(x, y)
            if d1 is None:
                break
            xm = x + sign * 0.5 * step_mm * d1[0]
            ym = y + sign * 0.5 * step_mm * d1[1]
            d2 = direction(xm, ym)
            if d2 is None:
                break
            xn = x + sign * step_mm * d2[0]
            yn = y + sign * step_mm * d2[1]
            if not inside(xn, yn):
                break
            if occ.closer_than(xn, yn, d_test):
                break
            if _self_hit(own, pts, xn, yn, d_test, bin_own, recent_gap):
                break
            key = (int(math.floor(xn / bin_own)), int(math.floor(yn / bin_own)))
            own.setdefault(key, []).append(len(pts))
            pts.append((xn, yn))
            x, y = xn, yn
        return pts

    seed_step = max(separation_mm / 2.0, 0.5)
    lines: list[Streamline] = []
    y_seed = box.y0
    while y_seed <= box.y1:
        x_seed = box.x0
        while x_seed <= box.x1:
            if (direction(x_seed, y_seed) is not None
                    and not occ.closer_than(x_seed, y_seed, separation_mm)):
                back = trace(x_seed, y_seed, -1.0)
                fwd = trace(x_seed, y_seed, 1.0)
                path = list(reversed(back)) + [(x_seed, y_seed)] + fwd
                length = _polyline_mm(path)
                if length >= min_length_mm:
                    for px, py in path:
                        occ.add(px, py)
                    arr = np.asarray(path, dtype=float)
                    lines.append(Streamline(x=arr[:, 0], y=arr[:, 1], length_mm=length))
            x_seed += seed_step
        y_seed += seed_step
    return lines


def streamlines(
    u: np.ndarray,
    v: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    *,
    frame,
    separation_mm: float = STREAM_SEPARATION_MM,
    max_paths: int = STREAM_MAX_PATHS,
    max_total_mm: float = STREAM_PAPER_BUDGET_MM,
    min_speed: float = STREAM_MIN_SPEED_MS,
    step_mm: float = 0.5,
    min_length_mm: float = STREAM_MIN_LENGTH_MM,
    tries: int = STREAM_MAX_TRIES,
) -> StreamResult:
    """Evenly spaced streamlines within the frame and the budget.

    ``frame`` is a :class:`backend.weather.render.Frame`. ``lat`` and ``lon``
    must be ascending and uniform. ``min_speed`` (m/s) is 0 by default, so every
    line with a wind direction is kept; a jet-only toggle passes a positive value.
    """
    lon_axis = np.asarray(lon, dtype=float)
    lat_axis = np.asarray(lat, dtype=float)
    ux_a, uy_a, speed_a = _direction_cells(np.asarray(u, float), np.asarray(v, float),
                                           lat_axis, float(frame.kx))
    ux = ux_a.tolist()
    uy = uy_a.tolist()
    speed = speed_a.tolist()

    # Budget search: a separation that is too small is over budget (lo), one
    # that fits is within budget (hi). The first over-budget pass grows the
    # separation by the worse ratio; once both bounds are known the search
    # bisects, and it stops at 5 percent. The densest fitting pass is kept.
    sep = float(separation_mm)
    lo: float | None = None
    hi: float | None = None
    best: tuple[list[Streamline], float] | None = None
    last: list[Streamline] = []
    thinned = False
    for attempt in range(max(1, tries)):
        lines = _one_pass(ux, uy, speed, lon_axis, lat_axis, frame, sep,
                          min_speed, step_mm, min_length_mm)
        total = total_length_mm(lines)
        last = lines
        if len(lines) <= max_paths and total <= max_total_mm:
            if attempt == 0:
                return StreamResult(lines=lines, separation_mm=sep, thinned=False, over_budget=False)
            best = (lines, sep)
            hi = sep
        else:
            lo = sep
            # Line count scales about with 1 / separation^2 and length about with
            # 1 / separation: step by the worse ratio, never less than 10 percent.
            ratio = max(len(lines) / max_paths, total / max_total_mm)
            grow = max(1.1, math.sqrt(ratio))
        thinned = True
        if best is not None and lo is not None and hi is not None:
            if hi - lo <= 0.05 * hi:
                break
            sep = (lo + hi) / 2.0
        elif lo is not None:
            sep = lo * grow
    if best is not None:
        return StreamResult(lines=best[0], separation_mm=best[1], thinned=thinned, over_budget=False)
    return StreamResult(lines=last, separation_mm=sep, thinned=thinned, over_budget=True)


def total_length_mm(lines: list[Streamline]) -> float:
    return float(sum(line.length_mm for line in lines))
