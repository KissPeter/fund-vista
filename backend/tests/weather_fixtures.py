"""Synthetic weather fields for the hermetic weather tests (no network).

A cyclone-like pressure field and a rotating wind field on a regular grid
with the Europe box spacing. Pure numpy; nothing here touches a store.
"""

from __future__ import annotations

import numpy as np

from backend.weather.config import EUROPE, GRID_STEP_DEG


def europe_axes(step: float = GRID_STEP_DEG) -> tuple[np.ndarray, np.ndarray]:
    """(lon, lat) ascending axes covering the Europe box."""
    lon = np.arange(EUROPE.lon0, EUROPE.lon1 + step / 2, step)
    lat = np.arange(EUROPE.lat0, EUROPE.lat1 + step / 2, step)
    return lon, lat


def grid(step: float = GRID_STEP_DEG):
    lon, lat = europe_axes(step)
    lon2, lat2 = np.meshgrid(lon, lat)
    return lon, lat, lon2, lat2


def gaussian(lon2, lat2, lon_c, lat_c, sigma_deg):
    return np.exp(-(((lon2 - lon_c) ** 2 + (lat2 - lat_c) ** 2) / (2 * sigma_deg**2)))


def cyclone_mslp(lows, highs=(), *, background=1013.0, sigma_deg=5.0, depth=25.0, step=GRID_STEP_DEG):
    """MSLP (hPa) with Gaussian lows ``[(lon, lat, depth)]`` and highs ``[(lon, lat, height)]``."""
    lon, lat, lon2, lat2 = grid(step)
    field = np.full(lon2.shape, background, dtype=float)
    for lo_lon, lo_lat, dep in lows:
        field -= dep * gaussian(lon2, lat2, lo_lon, lo_lat, sigma_deg)
    for hi_lon, hi_lat, hgt in highs:
        field += hgt * gaussian(lon2, lat2, hi_lon, hi_lat, sigma_deg)
    return lon, lat, field


def rotating_wind(step: float = GRID_STEP_DEG, *, jet_speed=40.0, vortex_speed=15.0):
    """(u, v) m/s: a cyclonic vortex over Europe plus a zonal jet near 55 N."""
    lon, lat, lon2, lat2 = grid(step)
    xc, yc = 10.0, 52.0
    k = vortex_speed / 6.0
    dx = lon2 - xc
    dy = lat2 - yc
    u = -k * dy * np.exp(-(dx**2 + dy**2) / (2 * 8.0**2))
    v = k * dx * np.exp(-(dx**2 + dy**2) / (2 * 8.0**2))
    u = u + jet_speed * np.exp(-(((lat2 - 55.0) / 5.0) ** 2))
    return lat, lon, u.astype(float), v.astype(float)
