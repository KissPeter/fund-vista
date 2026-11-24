"""H and L pressure centres (markers) on a smoothed MSLP grid.

A cell is a candidate when it is the extreme of its square window and passes
the value threshold (H at or above 1015 hPa, L at or below 1005 hPa). The
candidates are then thinned greedily, strongest first: a candidate within
``min_separation_km`` of an accepted marker of the same kind is dropped. That
removes the duplicate markers the POC produced inside one system (a flat
centre or two near-equal maxima in one window both won the window test).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from backend.weather.config import (
    EXTREMA_H_MIN_HPA,
    EXTREMA_L_MAX_HPA,
    MIN_EXTREMA_SEPARATION_KM,
)
from backend.weather.contours import gaussian_smooth
from backend.weather.geo import km_between

#: Smoothing sigma (cells) and search window (cells, odd) for the centres.
EXTREMA_SIGMA_CELLS = 2.0
EXTREMA_WINDOW_CELLS = 25
#: Cells left out at the grid edge, where the window is clipped.
EXTREMA_EDGE_CELLS = 4


@dataclass(frozen=True)
class Extremum:
    kind: str  # "H" or "L"
    lon: float
    lat: float
    value_hpa: float  # raw (unsmoothed) MSLP at the marker cell


def _window_extreme(field: np.ndarray, window: int, mode: str) -> np.ndarray:
    pad = window // 2
    padded = np.pad(field, pad, mode="edge")
    view = sliding_window_view(padded, (window, window))
    return view.max(axis=(2, 3)) if mode == "max" else view.min(axis=(2, 3))


def find_extrema(
    mslp_hpa: np.ndarray,
    lon: np.ndarray,
    lat: np.ndarray,
    *,
    min_separation_km: float = MIN_EXTREMA_SEPARATION_KM,
    h_min: float = EXTREMA_H_MIN_HPA,
    l_max: float = EXTREMA_L_MAX_HPA,
    sigma: float = EXTREMA_SIGMA_CELLS,
    window: int = EXTREMA_WINDOW_CELLS,
) -> list[Extremum]:
    """H and L markers, strongest first within each kind. Deterministic."""
    sm = gaussian_smooth(mslp_hpa, sigma)
    ny, nx = sm.shape
    edge = EXTREMA_EDGE_CELLS
    inner = np.zeros((ny, nx), dtype=bool)
    inner[edge:ny - edge, edge:nx - edge] = True
    is_max = (sm >= _window_extreme(sm, window, "max")) & (sm >= h_min) & inner
    is_min = (sm <= _window_extreme(sm, window, "min")) & (sm <= l_max) & inner

    result: list[Extremum] = []
    for kind, mask, order_sign in (("H", is_max, -1.0), ("L", is_min, 1.0)):
        idx = np.argwhere(mask)
        if idx.size == 0:
            continue
        vals = sm[idx[:, 0], idx[:, 1]] * order_sign
        # strongest first; ties by latitude then longitude for determinism
        order = np.lexsort((lon[idx[:, 1]], lat[idx[:, 0]], vals))
        accepted: list[Extremum] = []
        for k in order:
            i, j = int(idx[k, 0]), int(idx[k, 1])
            c_lon, c_lat = float(lon[j]), float(lat[i])
            if any(km_between(c_lon, c_lat, a.lon, a.lat) < min_separation_km for a in accepted):
                continue
            accepted.append(Extremum(kind, c_lon, c_lat, float(mslp_hpa[i, j])))
        result.extend(accepted)
    return result
