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


# -- r4: country border + paper-fill framing (aspect, min-path-length) --------


def _view(lat=LAT, lon=LON, half_deg=0.5):
    return (lat - half_deg, lat + half_deg, lon - half_deg, lon + half_deg)


def test_country_layer_rides_the_map_switch():
    """The border is a Map-layer group: on with Map, gone without it."""
    base = {"country": [[[14.0, 48.0], [22.5, 48.6]]], "motorways": [], "waterways": [],
            "water": [], "airfield": []}
    assert "country" in R.MAP_LAYERS and R.MAP_LAYERS[0] == "country"  # plotted under everything
    flt = Filters(t_from=0, t_to=24 * 3600)
    svg, _ = R.render_svg([], base, _BBOX, flt, AP)
    assert 'id="traffic-country"' in svg
    svg, _ = R.render_svg([], base, _BBOX, flt, AP, include_map=False)
    assert 'id="traffic-country"' not in svg
    svg, _ = R.render_svg([], base, _BBOX, flt, AP)
    assert 'inkscape:label="Country border"' in svg  # the group rides the Pen ink
    assert bm.country_query((1, 2, 3, 4)).count("relation") >= 2  # union: ISO + name tag paths
    assert "admin_level" in bm.country_query((1, 2, 3, 4)) and "out geom" in bm.country_query((1, 2, 3, 4))
    assert bm.layer_key("country", (1, 2, 3, 4)) != bm.layer_key("motorways", (1, 2, 3, 4))


def _d_points(svg: str) -> list[tuple[float, float]]:
    """The (x, y) vertices of the SVG's first drawn path."""
    coords = svg.split('d="M')[1].split('"')[0].replace("L", " ").split()
    return [(float(c.split(",")[0]), float(c.split(",")[1])) for c in coords]


def test_viewport_aspect_frames_canvas_and_centres():
    """The canvas takes exactly the given ratio; content is fit-contain centred."""
    # A line exactly spanning the view bbox — its clipped ends ARE the
    # content frame, so the letterbox margin shows as distance from the
    # canvas edges (a wider line would end outside the frame and lie).
    base = {"motorways": [[[LON - 0.5, LAT], [LON + 0.5, LAT]]]}
    flt = Filters(t_from=0, t_to=24 * 3600)
    svg, _ = R.render_svg([], base, _view(), flt, AP, viewport_aspect=297 / 210)
    assert svg.split('viewBox="')[1].split('"')[0] == "0 0 1000 707"  # A4-landscape shape
    xs = [x for x, _y in _d_points(svg)]
    assert 240 < min(xs) and max(xs) < 760  # the taller-than-wide view letterboxes left/right
    # A projected-square (radius) view on a portrait sheet letterboxes
    # top/bottom instead — the contain fit never distorts.
    square = R.view_bbox(LAT, LON, 25.0)
    svg_p, _ = R.render_svg([], base, square, flt, AP, viewport_aspect=210 / 297)
    assert svg_p.split('viewBox="')[1].split('"')[0] == "0 0 1000 1414"
    ys = [y for _x, y in _d_points(svg_p)]
    assert 200 < min(ys) and max(ys) < 800  # canvas 1000 x 1414, content centred
    # No aspect: the historical bbox-ratio canvas (square view → 1000 x 1000).
    svg_sq, _ = R.render_svg([], base, square, flt, AP)
    assert svg_sq.split('viewBox="')[1].split('"')[0] == "0 0 1000 1000"


def test_min_path_len_filters_every_layer():
    """Short pieces vanish on the basemap AND on movement traces; long stay."""
    # 25 km square view: 1 SVG unit ≈ 25 m, so a ~100 m line is ~4 units.
    small = R.view_bbox(LAT, LON, 25.0)
    short = [[LON, LAT], [LON + 100.0 / 111200 / math.cos(math.radians(LAT)), LAT]]
    long_ = [[LON, LAT], [LON + 3000.0 / 111200 / math.cos(math.radians(LAT)), LAT]]
    base = {"motorways": [short, long_], "waterways": [], "water": [], "airfield": [], "country": []}
    move = {"hex": "x", "type": "A320", "t": 100.0, "flight": "W1", "kind": "arr",
            "holding": False, "go_around": False, "db_flags": 0,
            "pts": [[100.0, LAT, LON, 0, 1],
                    [110.0, LAT + 100.0 / 111200, LON, 0, 1]]}  # a ~100 m hop
    flt = Filters(t_from=0, t_to=24 * 3600)
    svg, _ = R.render_svg([move], base, small, flt, AP)
    assert svg.count("<path") == 3  # 2 basemap + 1 movement — nothing filtered yet
    svg, _ = R.render_svg([move], base, small, flt, AP, min_path_len_m=500.0)
    assert svg.count("<path") == 1  # only the ~3 km motorway stays
    assert 'd="M' in svg and 'id="traffic-motorways"' in svg
    # 0 keeps the historical behaviour (same as omitting the knob).
    svg0, _ = R.render_svg([move], base, small, flt, AP, min_path_len_m=0.0)
    assert svg0.count("<path") == 3


def test_expand_view_to_aspect():
    """Grows one axis around the centre, clamped to the day bbox."""
    view = _view(half_deg=0.25)  # a ~55 km square-in-degrees box (taller than wide)
    limits = _view(half_deg=0.95)  # the day bbox (±1.9°/±3.6° would exceed the test's)
    square = R.view_bbox(LAT, LON, 25.0)  # a projected-square radius view (aspect 1.0)
    # Landscape: lon grows, lat untouched, centre kept, still inside the limits.
    s, n, w, e = R.expand_view_to_aspect(view, 297 / 210, limits)
    assert math.isclose((e - w) * math.cos(math.radians(LAT)) / (n - s), 297 / 210, rel_tol=1e-9)
    assert s == view[0] and n == view[1] and limits[2] <= w < view[2] < LON < view[3] < e <= limits[3]
    # Portrait on the square view: lat grows instead (the square is too wide).
    s, n, w, e = R.expand_view_to_aspect(square, 210 / 297, limits)
    assert math.isclose((e - w) * math.cos(math.radians(LAT)) / (n - s), 210 / 297, rel_tol=1e-9)
    assert w == square[2] and e == square[3] and limits[0] <= s < square[0] < LAT < square[1] < n <= limits[1]
    # A demand beyond the limits clamps and never leaves them.
    tiny_limits = _view(half_deg=0.26)
    s, n, w, e = R.expand_view_to_aspect(view, 297 / 210, tiny_limits)
    assert tiny_limits[2] <= w and e <= tiny_limits[3]
    # Already-matching aspect is returned untouched.
    assert R.expand_view_to_aspect(square, 1.0, limits) == square
    # A never-shrink guarantee: the grown view contains the original one.
    grown = R.expand_view_to_aspect(view, 297 / 210, tiny_limits)
    assert grown[0] <= view[0] and grown[1] >= view[1] and grown[2] <= view[2] and grown[3] >= view[3]


def test_render_request_framing_knobs():
    """min_path_len_m / viewport_aspect validate like the citymap knobs."""
    r = _req()
    assert r.min_path_len_m == 0.0 and r.viewport_aspect is None  # historical default
    assert _req(min_path_len_m=10.0).min_path_len_m == 10.0
    assert _req(viewport_aspect=297 / 210).viewport_aspect == pytest.approx(297 / 210)
    with pytest.raises(Exception):
        _req(min_path_len_m=100.1)  # above the citymap cap → 422
    with pytest.raises(Exception):
        _req(min_path_len_m=-0.1)  # negative → 422
    with pytest.raises(Exception):
        _req(viewport_aspect=0.0)  # gt=0 → 422
    with pytest.raises(Exception):
        _req(viewport_aspect=10.1)  # above cap → 422


def test_cache_fingerprint_covers_framing_knobs():
    """A min-length or aspect change must re-render, never serve stale art."""
    from backend.traffic.render_cache import _filter_fingerprint

    a = _req()
    b = _req(min_path_len_m=10.0)
    c = _req(viewport_aspect=1.414)
    fps = {_filter_fingerprint(x) for x in (a, b, c)}
    assert len(fps) == 3  # all three differ
    assert "minlen=0.0" in _filter_fingerprint(a) and "aspect=0.0000" in _filter_fingerprint(a)
