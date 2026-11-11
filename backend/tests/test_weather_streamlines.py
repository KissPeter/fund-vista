"""Streamlines in sheet millimetres: direction, determinism, coverage, minimum
length and the budget (thinning by separation, never by truncation)."""

from __future__ import annotations

import numpy as np

from backend.tests.weather_fixtures import rotating_wind
from backend.weather import config as C
from backend.weather.render import fit_frame
from backend.weather.streamlines import streamlines, total_length_mm


def _uniform(u0, v0, nlat=40, nlon=90):
    lon = np.linspace(-35.0, 55.0, nlon)
    lat = np.linspace(33.0, 71.0, nlat)
    u = np.full((nlat, nlon), float(u0))
    v = np.full((nlat, nlon), float(v0))
    return u, v, lat, lon


def _frame():
    return fit_frame(C.EUROPE, "landscape")


def test_zonal_flow_lines_stay_level_on_the_paper():
    u, v, lat, lon = _uniform(20.0, 0.0)
    res = streamlines(u, v, lat, lon, frame=_frame())
    assert res.lines, "a uniform eastward flow must yield lines"
    for ln in res.lines:
        assert np.allclose(ln.y, ln.y[0], atol=1e-6)
        assert ln.x[-1] != ln.x[0]


def test_meridional_flow_lines_stay_vertical_on_the_paper():
    u, v, lat, lon = _uniform(0.0, 15.0)
    res = streamlines(u, v, lat, lon, frame=_frame())
    assert res.lines
    for ln in res.lines:
        assert np.allclose(ln.x, ln.x[0], atol=1e-6)


def test_calm_field_yields_no_lines():
    u, v, lat, lon = _uniform(0.0, 0.0)
    assert streamlines(u, v, lat, lon, frame=_frame()).lines == []


def test_deterministic_output():
    lat, lon, u, v = rotating_wind()
    a = streamlines(u, v, lat, lon, frame=_frame())
    b = streamlines(u, v, lat, lon, frame=_frame())
    assert len(a.lines) == len(b.lines) > 0
    for x, y in zip(a.lines, b.lines):
        assert np.array_equal(x.x, y.x) and np.array_equal(x.y, y.y)


def test_every_line_is_at_least_the_minimum_length():
    lat, lon, u, v = rotating_wind()
    res = streamlines(u, v, lat, lon, frame=_frame())
    assert res.lines
    for ln in res.lines:
        assert ln.length_mm >= C.STREAM_MIN_LENGTH_MM - 1e-6


def test_coverage_on_a_6_by_4_grid_of_the_frame():
    """Cyclone plus jet: every cell of a 6x4 split of the frame has a line point."""
    lat, lon, u, v = rotating_wind()
    frame = _frame()
    res = streamlines(u, v, lat, lon, frame=frame)
    box = frame.box
    xs = np.concatenate([ln.x for ln in res.lines])
    ys = np.concatenate([ln.y for ln in res.lines])
    for i in range(6):
        for j in range(4):
            x0 = box.x0 + (box.x1 - box.x0) * i / 6
            x1 = box.x0 + (box.x1 - box.x0) * (i + 1) / 6
            y0 = box.y0 + (box.y1 - box.y0) * j / 4
            y1 = box.y0 + (box.y1 - box.y0) * (j + 1) / 4
            hit = np.any((xs >= x0) & (xs < x1) & (ys >= y0) & (ys < y1))
            assert hit, f"cell {i},{j} has no streamline"


def test_budget_holds_on_the_europe_fixture():
    lat, lon, u, v = rotating_wind()
    res = streamlines(u, v, lat, lon, frame=_frame())
    assert not res.over_budget
    assert len(res.lines) <= C.STREAM_MAX_PATHS
    assert total_length_mm(res.lines) <= C.STREAM_PAPER_BUDGET_MM
    print(f"\nEUROPE FIXTURE streamlines: paths={len(res.lines)} "
          f"total_mm={total_length_mm(res.lines):.0f} separation={res.separation_mm:.2f}")


def test_too_dense_start_is_thinned_with_a_flag():
    lat, lon, u, v = rotating_wind()
    res = streamlines(u, v, lat, lon, frame=_frame(), separation_mm=2.0)
    assert res.thinned
    assert res.separation_mm > 2.0
    assert len(res.lines) <= C.STREAM_MAX_PATHS


def test_over_budget_reports_the_separation_actually_used(monkeypatch):
    """When no pass fits the budget, the reported separation is the one the
    last pass ran with, not the untested next value."""
    from backend.weather import streamlines as S

    used: list[float] = []
    real_pass = S._one_pass

    def spy(*args):
        used.append(args[6])  # the separation argument of _one_pass
        return real_pass(*args)

    monkeypatch.setattr(S, "_one_pass", spy)
    lat, lon, u, v = rotating_wind()
    res = streamlines(u, v, lat, lon, frame=_frame(), max_paths=1,
                      max_total_mm=1.0, tries=3)
    assert res.over_budget
    assert len(used) == 3
    assert res.separation_mm == used[-1]

