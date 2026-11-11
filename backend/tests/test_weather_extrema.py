"""H/L markers: one marker per system, value thresholds, and the duplicate fix."""

from __future__ import annotations

from backend.tests.weather_fixtures import cyclone_mslp
from backend.weather.extrema import find_extrema
from backend.weather.geo import km_between


def _kinds(markers, kind):
    return [m for m in markers if m.kind == kind]


def test_single_low_one_l_marker_near_centre():
    lon, lat, field = cyclone_mslp([(10.0, 52.0, 25.0)], sigma_deg=5.0)
    markers = find_extrema(field, lon, lat)
    lows = _kinds(markers, "L")
    assert len(lows) == 1
    assert km_between(lows[0].lon, lows[0].lat, 10.0, 52.0) < 150.0
    assert lows[0].value_hpa < 1000.0


def test_two_minima_inside_one_system_give_one_marker():
    # Two equal lows 1 degree apart: the POC gave two L markers here.
    lon, lat, field = cyclone_mslp([(8.0, 52.0, 25.0), (9.0, 52.0, 25.0)], sigma_deg=2.0)
    lows = _kinds(find_extrema(field, lon, lat), "L")
    assert len(lows) == 1


def test_two_distant_lows_keep_two_markers():
    lon, lat, field = cyclone_mslp([(-15.0, 52.0, 25.0), (8.5, 52.0, 25.0)], sigma_deg=4.0)
    assert len(_kinds(find_extrema(field, lon, lat), "L")) == 2


def test_threshold_rules_no_h_on_normal_pressure():
    lon, lat, field = cyclone_mslp([], highs=[], sigma_deg=5.0)
    assert find_extrema(field, lon, lat) == []


def test_high_pressure_gives_h_marker():
    lon, lat, field = cyclone_mslp([], highs=[(30.0, 60.0, 12.0)], sigma_deg=5.0)
    highs = _kinds(find_extrema(field, lon, lat), "H")
    assert len(highs) == 1
    assert highs[0].value_hpa >= 1015.0
