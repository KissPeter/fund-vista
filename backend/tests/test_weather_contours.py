"""Contour count, short and tiny line filtering, and the smoothing kernel."""

from __future__ import annotations

import numpy as np

from backend.weather.config import MIN_CONTOUR_LENGTH_KM, MSLP_STEP_HPA
from backend.weather.contours import contour_lines, gaussian_smooth, levels_between
from backend.weather.geo import ring_area_km2
from backend.tests.weather_fixtures import cyclone_mslp


def test_smoothing_keeps_constant_and_flattens_spike():
    flat = np.full((20, 30), 1013.0)
    assert np.allclose(gaussian_smooth(flat, 1.0), 1013.0)
    spike = np.zeros((21, 21))
    spike[10, 10] = 10.0
    out = gaussian_smooth(spike, 1.0)
    assert out[10, 10] < 10.0 and out.sum() > 0.0
    assert np.isclose(out.sum(), 10.0, rtol=1e-3)  # mass preserved (edge zero)
    assert np.array_equal(gaussian_smooth(spike, 0.0), spike)


def test_levels_cover_range_on_step():
    field = np.array([[1001.3, 1030.2]])
    lv = levels_between(field, MSLP_STEP_HPA)
    assert lv[0] <= 1001.3 and lv[-1] >= 1030.2
    assert np.allclose(np.diff(lv), MSLP_STEP_HPA)


def test_single_low_gives_one_closed_ring_per_level():
    # Depth 20 hPa under 1013, sigma 8 deg. Levels 996..1008 close inside the
    # box; 1012 would need a ring about 20 deg wide, which runs off the edge.
    lon, lat, field = cyclone_mslp([(10.0, 52.0, 20.0)], sigma_deg=8.0)
    levels = levels_between(field, MSLP_STEP_HPA)
    lines = contour_lines(field, lon, lat, levels, heavy_every=5)
    closed = [ln for ln in lines if ln.closed]
    assert sorted({ln.level for ln in closed}) == [996.0, 1000.0, 1004.0, 1008.0]
    assert len(closed) == 4
    assert all(ln.length_km >= MIN_CONTOUR_LENGTH_KM for ln in lines)
    assert [ln.level for ln in lines if ln.heavy] == [1000.0]


def test_flat_field_has_no_lines():
    lon, lat, field = cyclone_mslp([], sigma_deg=8.0)
    assert contour_lines(field, lon, lat, levels_between(field, MSLP_STEP_HPA)) == []


def test_tiny_rings_are_dropped():
    # A narrow, shallow bump under the grid resolution: its ring is tiny.
    lon, lat, field = cyclone_mslp([], sigma_deg=8.0)
    lon2, lat2 = np.meshgrid(lon, lat)
    field = field - 3.0 * np.exp(-((lon2 - 10.0) ** 2 + (lat2 - 52.0) ** 2) / (2 * 0.12**2))
    lines = contour_lines(field, lon, lat, np.array([1010.0]), min_length_km=0.0, min_area_km2=0.0)
    kept = contour_lines(field, lon, lat, np.array([1010.0]))
    assert len(kept) <= len(lines)
    for ln in kept:
        if ln.closed:
            assert ring_area_km2(ln.lon[:-1], ln.lat[:-1]) >= 2000.0
