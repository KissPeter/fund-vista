"""Streamlines: determinism, direction on the equirectangular map, and the line budget."""

from __future__ import annotations

import numpy as np

from backend.tests.weather_fixtures import rotating_wind, europe_axes
from backend.weather.config import STREAM_MAX_PATHS, STREAM_MAX_TOTAL_KM
from backend.weather.streamlines import streamlines, total_length_km


def _uniform(u0, v0, nlat=40, nlon=90):
    lon = np.linspace(-35.0, 55.0, nlon)
    lat = np.linspace(33.0, 71.0, nlat)
    u = np.full((nlat, nlon), float(u0))
    v = np.full((nlat, nlon), float(v0))
    return u, v, lat, lon


def test_zonal_flow_lines_run_east_and_stay_level():
    u, v, lat, lon = _uniform(20.0, 0.0)
    lines = streamlines(u, v, lat, lon, separation=3.0, max_paths=20)
    assert lines, "a uniform eastward flow must yield lines"
    for ln in lines:
        assert ln.lon[-1] - ln.lon[0] > 0 or ln.lon[0] - ln.lon[-1] > 0
        assert np.allclose(ln.lat, ln.lat[0], atol=1e-4)


def test_meridional_flow_lines_run_north():
    u, v, lat, lon = _uniform(0.0, 15.0)
    lines = streamlines(u, v, lat, lon, separation=3.0, max_paths=20)
    assert lines
    for ln in lines:
        assert np.allclose(ln.lon, ln.lon[0], atol=1e-4)


def test_calm_field_yields_no_lines():
    u, v, lat, lon = _uniform(0.0, 0.0)
    assert streamlines(u, v, lat, lon) == []


def test_deterministic_output():
    lat, lon, u, v = rotating_wind()
    a = streamlines(u, v, lat, lon)
    b = streamlines(u, v, lat, lon)
    assert len(a) == len(b) > 0
    for x, y in zip(a, b):
        assert np.array_equal(x.lon, y.lon) and np.array_equal(x.lat, y.lat)


def test_budget_paths_and_length_hold_on_europe_fixture():
    lat, lon, u, v = rotating_wind()
    lines = streamlines(u, v, lat, lon)
    assert len(lines) <= STREAM_MAX_PATHS
    assert total_length_km(lines) <= STREAM_MAX_TOTAL_KM
    print(f"\nEUROPE FIXTURE streamlines: paths={len(lines)} total_km={total_length_km(lines):.0f}")


def test_path_cap_is_hard():
    lat, lon, u, v = rotating_wind()
    lines = streamlines(u, v, lat, lon, max_paths=10)
    assert len(lines) == 10


def test_length_cap_is_hard():
    lat, lon, u, v = rotating_wind()
    lines = streamlines(u, v, lat, lon, max_total_km=3000.0)
    assert total_length_km(lines) <= 3000.0
    assert europe_axes() is not None
