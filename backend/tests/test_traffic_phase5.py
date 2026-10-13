"""Traffic Phase 5 pure unit tests (TC-D46–D49, no app, no network).

Extra filters (§4 items 1–6): aircraft size categories, runway flow,
altitude band, category pen mode, window stats. Endpoint behavior lives in
``test_traffic_api.py`` (TC-D50, real HTTP).
"""

from __future__ import annotations

import datetime as dt
import math

import pytest

from backend.traffic.aircraft import CATEGORIES, category_of
from backend.traffic.classify import (
    CLASSIFIER_VERSION,
    holding_span_s,
    runway_flow,
)
from backend.traffic.filters import Filters
from backend.traffic.filters import pen as pen_of
from backend.traffic.schemas import RenderRequest
from backend.traffic.stats import movement_stats

LAT, LON, ELEV = 47.4369, 19.2556, 495.0
AP = (LAT, LON, ELEV)


def _along(hdg_deg: float, dist_km: float, alts, t0: float = 1_000_000.0, step: float = 60.0):
    """Straight leg flying ``hdg_deg`` toward the airport, ``dist_km`` out.

    ``alts`` is one MSL altitude per point (last point may be 0 = ground).
    """
    n = len(alts)
    h = math.radians(hdg_deg)
    pts = []
    for i, alt in enumerate(alts):
        d = dist_km * (1 - i / (n - 1))
        pts.append([t0 + i * step,
                    LAT - (d * math.cos(h)) / 111.2,
                    LON - (d * math.sin(h)) / (111.2 * math.cos(math.radians(LAT))),
                    alt, 1 if alt == 0 else 0])
    return pts


def _arrival(hdg: float = 130.0, dist: float = 30.0, alt: float = 6000.0, n: int = 10):
    return _along(hdg, dist, [alt] * (n - 1) + [0], step=60.0)


def _mov(**kw):
    m = {"hex": "x", "type": "A320", "t": 1_000_000.0, "flight": "WZZ1",
         "kind": "arr", "holding": False, "holding_s": 0, "flow": "13",
         "go_around": False, "db_flags": 0, "pts": _arrival()}
    m.update(kw)
    return m


# TC-D46: size categories ----------------------------------------------------

def test_category_wide():
    for code in ("A333", "B77W", "A388", "B748"):
        assert category_of(code) == "wide"


def test_category_narrow():
    for code in ("A320", "B738", "A20N", "BCS3"):
        assert category_of(code) == "narrow"


def test_category_regional():
    for code in ("E190", "AT72", "DH8D", "CRJ9"):
        assert category_of(code) == "regional"


def test_category_other_for_unknown_and_missing():
    for code in ("H60", "C172", "XXX", "", None):
        assert category_of(code) == "other"


def test_category_normalises_case_and_whitespace():
    assert category_of("  a320 ") == "narrow"
    assert set(CATEGORIES) == {"wide", "narrow", "regional", "other"}


# TC-D47: runway flow + holding span -----------------------------------------

def test_flow_arrival_touchdown_heading():
    assert runway_flow(_arrival(hdg=130.0), "arr") == "13"


def test_flow_departure_liftoff_heading():
    leg = [p[:] for p in reversed(_arrival(hdg=130.0))]
    leg[0][3], leg[0][4] = 0, 1
    for p in leg[1:]:
        p[4] = 0
    assert runway_flow(leg, "dep") == "31"  # reciprocal of the 130 approach


def test_flow_rounding_boundaries():
    assert runway_flow(_arrival(hdg=124.0), "arr") == "12"
    assert runway_flow(_arrival(hdg=125.0), "arr") == "13"


def test_flow_wraps_at_north():
    assert runway_flow(_arrival(hdg=359.0), "arr") == "36"
    assert runway_flow(_arrival(hdg=0.0), "arr") == "36"


def test_flow_without_ground_transition_uses_end_segment():
    leg = _along(130.0, 30.0, [6000.0] * 10)  # never touches: no GND flags
    assert runway_flow(leg, "arr") == "13"


def _holding_circle(t0: float = 2_000_000.0):
    """390° of turn ~20 km out (all points outside the 12 km circuit).

    14 points: any 13 consecutive ones span exactly 720 s with ≈360° of
    same-direction turn, so the hold fits the 12-minute detection window
    (13 points would only accumulate ≈330° of heading *changes*).
    """
    pts = []
    for i in range(14):
        a = math.radians(i * 30.0)
        pts.append([t0 + i * 60.0,
                    LAT + (20.0 + 3.0 * math.cos(a)) / 111.2,
                    LON + (3.0 * math.sin(a)) / (111.2 * math.cos(math.radians(LAT))),
                    4000.0, 0])
    return pts


def test_holding_span_full_circle():
    assert holding_span_s(_holding_circle(), LAT, LON) == 720.0


def test_holding_span_straight_leg_is_zero():
    assert holding_span_s(_arrival(), LAT, LON) == 0.0


def test_classify_day_gains_flow_and_holding_span():
    from backend.traffic.classify import classify_day

    assert CLASSIFIER_VERSION != "c1"  # Phase 5 shape bump trips the L2 key
    day = {"airport": {"lat": LAT, "lon": LON, "elev_ft": ELEV},
           "flights": [dict(hex="a0", reg=None, type="A320", desc=None, operator=None,
                             db_flags=0, calls=[[0, "WZZ1"]], pts=_arrival())]}
    (m,) = classify_day(day)
    assert m["flow"] == "13" and m["holding"] is False and m["holding_s"] == 0


def test_classify_day_holding_carries_span():
    from backend.traffic.classify import classify_day

    circle = _holding_circle()
    # continue into a touchdown with no time gap (same leg) and no climb
    tail = _along(130.0, 10.0, [3000.0, 2000.0, 1000.0, 500.0, 200.0, 0.0, 0.0, 0.0],
                  t0=circle[-1][0] + 60.0, step=60.0)
    day = {"airport": {"lat": LAT, "lon": LON, "elev_ft": ELEV},
           "flights": [dict(hex="b0", reg=None, type="A320", desc=None, operator=None,
                             db_flags=0, calls=[[0, "WZZ2"]], pts=circle + tail)]}
    (m,) = classify_day(day)
    assert m["holding"] is True and m["holding_s"] == 720


# TC-D48: pen gates + request validation -------------------------------------

def test_pen_category_gate():
    f = Filters(t_from=999_000, t_to=999_000 + 86_400, type_categories={"narrow"})
    assert pen_of(_mov(), f, AP) == "arrivals"
    assert pen_of(_mov(type="A333"), f, AP) is None
    assert pen_of(_mov(type="A320"), Filters(t_from=999_000, t_to=999_000 + 86_400), AP) == "arrivals"


def test_pen_flow_gate():
    f = Filters(t_from=999_000, t_to=999_000 + 86_400, flows={"31"})
    assert pen_of(_mov(), f, AP) is None  # flow 13 ≠ 31
    assert pen_of(_mov(flow="31"), f, AP) == "arrivals"


def test_pen_altitude_band_far_points_decide():
    flat_high = _along(130.0, 30.0, [8000.0] * 9 + [0])  # min far AGL 7505
    low_far = _along(130.0, 30.0, [2500.0] * 9 + [0])    # min far AGL 2005
    f = Filters(t_from=999_000, t_to=999_000 + 86_400, alt_max_agl_ft=3000)
    assert pen_of(_mov(pts=flat_high), f, AP) is None
    assert pen_of(_mov(pts=low_far), f, AP) == "arrivals"


def test_pen_altitude_band_boundary():
    pts = _along(130.0, 30.0, [8000.0] * 9 + [0])  # min far AGL exactly 7505
    base = dict(t_from=999_000, t_to=999_000 + 86_400)
    assert pen_of(_mov(pts=pts), Filters(alt_max_agl_ft=7505, **base), AP) == "arrivals"
    assert pen_of(_mov(pts=pts), Filters(alt_max_agl_ft=7504, **base), AP) is None


def test_pen_altitude_band_local_traffic_falls_back():
    near = _along(130.0, 5.0, [1500.0] * 9 + [0])  # nothing beyond 12 km
    f = Filters(t_from=999_000, t_to=999_000 + 86_400, alt_max_agl_ft=3000)
    assert pen_of(_mov(pts=near), f, AP) == "arrivals"
    assert pen_of(_mov(pts=near), f) is None  # band set but no airport → fail closed


def test_pen_category_mode_layers_and_priority():
    f = Filters(t_from=999_000, t_to=999_000 + 86_400, pen_mode="category")
    assert pen_of(_mov(), f, AP) == "narrow"
    assert pen_of(_mov(type="A333"), f, AP) == "wide"
    assert pen_of(_mov(type="H60"), f, AP) == "other"
    both = _mov(holding=True, go_around=True)
    assert pen_of(both, f, AP) == "go_around"  # highlight priority unchanged
    assert pen_of(_mov(holding=True), f, AP) == "holding"


def test_render_request_phase5_validation():
    base = {"icao": "LHBP", "date": "2026-10-10"}
    ok = RenderRequest(**dict(base, pen_mode="category", type_categories=["narrow"],
                              flows=["13", "01", "36"], alt_max_agl_ft=3000))
    assert ok.pen_mode == "category"
    assert RenderRequest(**base).pen_mode == "kind"  # default unchanged
    with pytest.raises(Exception, match="pen_mode"):
        RenderRequest(**dict(base, pen_mode="size"))
    with pytest.raises(Exception, match="unknown aircraft categories"):
        RenderRequest(**dict(base, type_categories=["jumbo"]))
    for bad_flow in ("99", "7", "AB", "37", "00"):
        with pytest.raises(Exception, match="runway numbers"):
            RenderRequest(**dict(base, flows=[bad_flow]))
    for bad_alt in (0, -100, 60001):
        with pytest.raises(Exception):
            RenderRequest(**dict(base, alt_max_agl_ft=bad_alt))


# TC-D49: window stats ---------------------------------------------------------

def _ts(hh: int, mm: int) -> float:
    return dt.datetime(2026, 10, 10, hh, mm, tzinfo=dt.timezone.utc).timestamp()


def test_stats_full_window():
    window = [
        _mov(t=_ts(6, 10)),
        _mov(t=_ts(6, 40), kind="dep", flow="31"),
        _mov(t=_ts(7, 5), kind="dep", flow="31", go_around=False),
        _mov(t=_ts(6, 50), go_around=True),
    ]
    assert movement_stats(window) == {
        "movements": 4, "arrivals": 2, "departures": 2,
        "go_arounds": 1, "go_around_rate": 0.25,
        "holdings": 0, "holding_minutes": 0, "busiest_hour": "06:00",
    }


def test_stats_holding_minutes_and_tie_break():
    window = [
        _mov(t=_ts(6, 10), holding=True, holding_s=720),
        _mov(t=_ts(7, 10), holding=True, holding_s=61),
    ]
    stats = movement_stats(window)
    assert stats["holdings"] == 2 and stats["holding_minutes"] == 13  # 781 s → 13 min
    assert stats["busiest_hour"] == "06:00"  # tie 1–1 → earliest hour


def test_stats_empty_window():
    assert movement_stats([]) == {
        "movements": 0, "arrivals": 0, "departures": 0,
        "go_arounds": 0, "go_around_rate": 0.0,
        "holdings": 0, "holding_minutes": 0, "busiest_hour": None,
    }


def test_stats_tolerates_pre_c2_movements():
    m = _mov(t=_ts(6, 10))
    del m["holding_s"]
    assert movement_stats([m])["holding_minutes"] == 0
