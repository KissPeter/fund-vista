"""Hermetic inputs for the weather render tests: synthetic Europe fields, a tiny
geography and a fake field reader. Nothing here touches the network or the store."""

from __future__ import annotations

import datetime as dt

import numpy as np

from backend.weather.config import EUROPE, Region
from backend.weather.fields import FieldSubset
from backend.weather.geography import Geography

from backend.tests.weather_fixtures import cyclone_mslp, europe_axes, grid, rotating_wind

WHEN = dt.datetime(2005, 5, 20, 12)


def _z500_t500():
    lon, lat, lon2, lat2 = grid()
    gauss = np.exp(-(((lon2 - 10.0) ** 2 + (lat2 - 52.0) ** 2) / (2 * 8.0**2)))
    z500 = 5560.0 + 0.9 * (lat2 - 50.0) - 120.0 * gauss
    t500 = -18.0 + 0.45 * (lat2 - 33.0) + 2.0 * np.sin(np.radians(lon2 * 3.0))
    return z500.astype(float), t500.astype(float)


def fixture_fields() -> dict[str, np.ndarray]:
    """Every field the layers can need, on the Europe grid (nlat, nlon)."""
    lon, lat, mslp = cyclone_mslp([(10.0, 52.0, 25.0), (-20.0, 60.0, 15.0)], highs=[(30.0, 45.0, 20.0)])
    lat_w, lon_w, u, v = rotating_wind()
    z500, t500 = _z500_t500()
    return {"mslp_hpa": mslp, "u250": u, "v250": v, "z500_gpm": z500, "t500_c": t500}


def fixture_subset(fields=None) -> FieldSubset:
    lon, lat = europe_axes()
    all_fields = fixture_fields()
    names = all_fields.keys() if fields is None else fields
    picked = {name: all_fields[name] for name in names}
    return FieldSubset(time=WHEN, region=EUROPE.name, lat=lat.astype(np.float32),
                       lon=lon.astype(np.float32), **picked)


class FakeReader:
    """Injected FieldReader: returns the fixture subset and records each call."""

    def __init__(self) -> None:
        self.calls: list[frozenset[str]] = []

    def read(self, when, region: Region, fields):
        self.calls.append(frozenset(fields))
        return fixture_subset(fields)


def fixture_geo() -> Geography:
    coast = np.array([[-30, 35], [-10, 40], [0, 50], [20, 60], [40, 50], [50, 40], [-30, 35]], dtype=float)
    borders = [
        np.array([[0, 35], [0, 71]], dtype=float),
        np.array([[-35, 50], [55, 50]], dtype=float),
    ]
    return Geography(coast=[coast], borders=borders)
