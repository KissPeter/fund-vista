"""Traffic Phase 2 pure unit tests (TC-D45, no app, no network).

Render geometry, basemap query/key builders, schema validation
equivalence classes + boundaries. Endpoint behavior lives in
``test_traffic_api.py`` (real HTTP).
"""

from __future__ import annotations

import datetime as dt
import math

import pytest

from backend.traffic import basemap as bm
from backend.traffic import render as R
from backend.traffic.filters import Filters, pen
from backend.traffic.schemas import RenderRequest

LAT, LON, ELEV = 47.4369, 19.2556, 495.0
AP = {"lat": LAT, "lon": LON, "elev_ft": ELEV}


def _leg(points, t0=1_000_000.0, step=10.0):
    return [[t0 + i * step, la, lo, alt, 1 if alt == 0 else 0] for i, (la, lo, alt) in enumerate(points)]


def _approach(n=60, start_km=60.0):
    pts = []
    for i in range(n):
        f = 1 - i / (n - 1)
        d = start_km * f / 111.2
        alt = 0 if i == n - 1 else ELEV + 300 + 9000 * f
        pts.append((LAT + d * 0.7, LON - d * 0.7, alt))
    return pts


def test_render_layers_and_zoom_clip():
    from backend.traffic.classify import classify_day

    bbox = (LAT - 0.5, LAT + 0.5, LON - 0.7, LON + 0.7)
    base = {"motorways": [[[LON - 1, LAT], [LON + 1, LAT]]], "waterways": [], "water": [], "airfield": []}
    day = {"airport": AP, "flights": [dict(hex="a0", reg=None, type="A321", desc=None, operator=None,
                                           calls=[[0, "WZZ1"]], pts=_leg(_approach()))]}
    moves = classify_day(day)
    svg, counts = R.render_svg(moves, base, bbox, Filters(t_from=999_000, t_to=999_000 + 86_400), AP)
    assert counts["arrivals"] == 1
    for layer in ("motorways", "waterways", "water", "airfield", "departures", "arrivals", "holding", "go_around"):
        assert 'id="traffic-%s"' % layer in svg
    assert "<text" not in svg  # chrome-free: attribution lives in API metadata
    assert all(0 <= float(n) <= 1000.001 for n in
               [c.split(",")[0] for c in svg.split('d="M')[1].split('"')[0].replace("L", " ").split()])


def test_view_bbox_and_projection():
    s, n, w, e = R.view_bbox(LAT, LON, 25.0)
    assert s < LAT < n and w < LON < e
    assert math.isclose(n - s, 50.0 / 111.2, rel_tol=1e-9)
    proj = R.Projection((s, n, w, e))
    x, y = proj.xy(LAT, LON)
    assert 0 < x < proj.width and 0 < y < proj.height


def test_overpass_geom_parsing():
    els = [{"type": "way", "geometry": [{"lat": 1, "lon": 2}, {"lat": 3, "lon": 4}]},
           {"type": "relation", "members": [{"type": "way", "geometry": [{"lat": 5, "lon": 6}, {"lat": 7, "lon": 8}]},
                                            {"type": "node"}]}]
    assert bm.polylines(els) == [[[2, 1], [4, 3]], [[6, 5], [8, 7]]]
    assert 'water"~"^(lake|reservoir)$' in bm.water_query((1, 2, 3, 4))
    for q in (bm.motorway_query((1, 2, 3, 4)), bm.waterway_query((1, 2, 3, 4))):
        assert "out geom" in q and "1.00000" in q  # south,west,north,east order


def test_quantise_snaps_outward_and_stable():
    a = (47.43691, 47.53699, 19.25551, 19.35559)
    q = bm.quantise(a)
    assert q[0] <= a[0] and q[1] >= a[1] and q[2] <= a[2] and q[3] >= a[3]
    b = (a[0] + 0.00005, a[1] - 0.00005, a[2] + 0.00005, a[3] - 0.00005)
    assert bm.quantise(b) == q  # near-identical pans share the L3 key
    assert bm.layer_key("motorways", a) == bm.layer_key("motorways", b)
    assert bm.layer_key("motorways", a) != bm.layer_key("water", a)


def test_window_boundary_t_to_excluded():
    m = {"hex": "x", "type": "A320", "t": 100.0, "flight": "W1",
         "kind": "arr", "holding": False, "go_around": False, "db_flags": 0,
         "pts": [[100.0, LAT, LON, 0, 1]]}
    assert pen(m, Filters(t_from=90, t_to=100)) is None  # [from, to)
    assert pen(m, Filters(t_from=100, t_to=110)) == "arrivals"
    assert pen(m, Filters(t_from=0, t_to=24 * 3600)) == "arrivals"  # full 24 h ok


def test_pen_priority_go_around_over_holding():
    base = {"hex": "x", "type": "A320", "t": 5.0, "flight": "W1",
            "kind": "arr", "db_flags": 0, "pts": []}
    both = dict(base, holding=True, go_around=True)
    assert pen(both, Filters(t_from=0, t_to=10)) == "go_around"


# -- issue #1080: the potential counts + the Map / Airport layer switches -------

_BBOX = (LAT - 0.5, LAT + 0.5, LON - 0.7, LON + 0.7)


def _movement(kind="arr", holding=False, go_around=False, t=100.0):
    return {"hex": "x", "type": "A320", "t": t, "flight": "W1", "kind": kind,
            "holding": holding, "go_around": go_around, "db_flags": 0,
            "pts": [[t, LAT, LON, 0, 1], [t + 10, LAT + 0.01, LON + 0.01, 0, 1]]}


def test_counts_are_potential_not_drawn():
    """The counts are counted BEFORE the layer filter (issue #1080).

    A layer that is switched off still reports how many lines it would
    contribute — the chip number never changes with the tick state.
    """
    moves = [_movement("arr"), _movement("dep"),
             _movement("arr", holding=True), _movement("dep", go_around=True)]
    base = {"motorways": [], "waterways": [], "water": [], "airfield": []}
    all_off = Filters(t_from=0, t_to=24 * 3600, arrivals=False, departures=False,
                      holding=False, go_around=False)
    svg, counts = R.render_svg(moves, base, _BBOX, all_off, AP)
    assert counts == {"arrivals": 1, "departures": 1, "holding": 1, "go_around": 1}
    assert "<path" not in svg  # nothing is drawn — the counts are potential only
    # The all-on render draws the same distribution the counts report.
    svg, counts = R.render_svg(moves, base, _BBOX, Filters(t_from=0, t_to=24 * 3600), AP)
    assert counts == {"arrivals": 1, "departures": 1, "holding": 1, "go_around": 1}
    assert svg.count("<path") == 4


def test_counts_ignore_only_the_layer_filter():
    """A movement dropped by another filter (here: the window) is not counted."""
    moves = [_movement("arr", t=100.0), _movement("arr", t=500.0)]
    base = {"motorways": [], "waterways": [], "water": [], "airfield": []}
    _svg, counts = R.render_svg(moves, base, _BBOX, Filters(t_from=0, t_to=200), AP)
    assert counts == {"arrivals": 1, "departures": 0, "holding": 0, "go_around": 0}


def test_map_and_airport_switches_drop_their_groups():
    moves = [_movement("arr")]
    base = {"motorways": [[[LON - 1, LAT], [LON + 1, LAT]]], "waterways": [], "water": [],
            "airfield": [[[LON - 1, LAT], [LON + 1, LAT]]]}
    flt = Filters(t_from=0, t_to=24 * 3600)
    svg, _ = R.render_svg(moves, base, _BBOX, flt, AP)
    assert 'id="traffic-motorways"' in svg and 'id="traffic-airfield"' in svg
    assert "<circle" in svg  # the airport marker circle
    # Map off: the three basemap groups are gone, the airfield stays.
    svg, _ = R.render_svg(moves, base, _BBOX, flt, AP, include_map=False)
    assert 'id="traffic-motorways"' not in svg
    assert 'id="traffic-waterways"' not in svg
    assert 'id="traffic-water"' not in svg
    assert 'id="traffic-airfield"' in svg
    assert "<circle" in svg
    # Airport off: the airfield group AND the marker circle are gone.
    svg, _ = R.render_svg(moves, base, _BBOX, flt, AP, include_airport=False)
    assert 'id="traffic-airfield"' not in svg
    assert "<circle" not in svg
    assert 'id="traffic-motorways"' in svg
    # Both off: only the movement layers stay.
    svg, _ = R.render_svg(moves, base, _BBOX, flt, AP, include_map=False, include_airport=False)
    assert 'id="traffic-motorways"' not in svg and 'id="traffic-airfield"' not in svg
    assert 'id="traffic-arrivals"' in svg


def _req(**kw):
    d = {"icao": "LHBP", "date": "2026-10-10"}
    d.update(kw)
    return RenderRequest(**d)


def test_render_request_defaults_all_on_full_day():
    r = _req()
    assert (r.t_from, r.t_to) == ("00:00", "24:00")
    assert r.arrivals and r.departures and r.holding and r.go_around
    assert r.map and r.airport  # issue #1080: the Map / Airport layers default on
    assert r.width == 1000
    f, t = r.day_range(dt.date(2026, 10, 10))
    assert t - f == 24 * 3600  # exactly 24 h is allowed


def test_render_request_window_rejections():
    with pytest.raises(Exception, match="after t_from"):
        _req(t_from="06:00", t_to="06:00")  # equal → 422
    with pytest.raises(Exception, match="after t_from"):
        _req(t_from="18:00", t_to="06:00")  # reversed → 422
    with pytest.raises(Exception, match="HH:MM"):
        _req(t_from="6:00")  # malformed → 422
    with pytest.raises(Exception, match="HH:MM"):
        _req(t_to="24:01")  # past end of day → 422


def test_render_request_view_rules():
    with pytest.raises(Exception, match="all set or all omitted"):
        _req(view_south=47.0)  # partial → 422
    with pytest.raises(Exception, match="below view_north"):
        _req(view_south=48.0, view_north=47.0, view_west=19.0, view_east=20.0)
    with pytest.raises(Exception, match="below view_east"):
        _req(view_south=47.0, view_north=48.0, view_west=20.0, view_east=19.0)
    ok = _req(view_south=47.0, view_north=48.0, view_west=19.0, view_east=20.0)
    assert ok.view_north == 48.0


def test_render_request_icao_width_forbid():
    with pytest.raises(Exception, match="Hungarian"):
        _req(icao="EGLL")
    with pytest.raises(Exception):
        _req(width=99)  # below min → 422
    with pytest.raises(Exception):
        _req(width=4001)  # above max → 422
    assert _req(width=100).width == 100 and _req(width=4000).width == 4000
    with pytest.raises(Exception):
        _req(icao="LHBP", date="2026-10-10", bogus=1)  # extra=forbid
