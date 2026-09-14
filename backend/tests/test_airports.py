"""Airports tests: geometry, OurAirports fallback, query/split, render units.

All cases are hermetic — OurAirports/Overpass are never touched (CSV math
runs on synthetic rows, query shape is asserted as a string, rendering runs
on synthetic polygons). The only HTTP cases hit local validation/metadata
endpoints.
"""

from __future__ import annotations

import asyncio
import math

from backend.airports.cache import (
    airports_cache_key,
    cache_get,
    cache_set,
    configure_airports_redis,
)
from backend.airports.geometry import (
    primary_runway_index,
    project,
    rotate_point,
    rotation_for_heading,
    runway_heading_deg,
)
from backend.airports.ourairports import heading_from_ident, runway_endpoints
from backend.airports.overpass import (
    AEROWAY_CLASSES,
    build_airport_query,
    split_aeroway,
)
from backend.airports.render import render_diagram
from backend.penplot import imaging


def test_heading_from_ident():
    assert heading_from_ident("13L") == 130.0
    assert heading_from_ident("31R") == 310.0
    assert heading_from_ident("02") == 20.0
    assert heading_from_ident("36") == 0.0
    assert heading_from_ident(" 09c ") == 90.0
    assert heading_from_ident("XX") is None
    assert heading_from_ident("") is None
    assert heading_from_ident("99") is None


def test_runway_endpoints_prefer_authoritative_coords():
    row = {
        "le_ident": "13L",
        "he_ident": "31R",
        "length_ft": "3000",
        "width_ft": "150",
        "le_latitude_deg": "47.4395",
        "le_longitude_deg": "19.2510",
        "he_latitude_deg": "47.4342",
        "he_longitude_deg": "19.2602",
    }
    le, he, derived = runway_endpoints(row, 47.4369, 19.2556)
    assert derived is False
    assert le == (19.2510, 47.4395)
    assert he == (19.2602, 47.4342)


def test_runway_endpoints_fallback_from_ident_heading():
    """Missing le_/he_ coords -> center ± half length along ident heading."""
    row = {
        "le_ident": "13",
        "he_ident": "31",
        "length_ft": "3000",  # 914.4 m
        "width_ft": "",
        "le_latitude_deg": "",
        "le_longitude_deg": "",
        "he_latitude_deg": "",
        "he_longitude_deg": "",
    }
    le, he, derived = runway_endpoints(row, 47.4369, 19.2556)
    assert derived is True
    # HE end must sit ~half length (457 m) away at heading 130°.
    dx = (he[0] - 19.2556) * 111320.0 * math.cos(math.radians(47.4369))
    dy = (he[1] - 47.4369) * 110540.0
    assert abs(math.hypot(dx, dy) - 457.2) < 1.0
    assert abs(math.degrees(math.atan2(dx, dy)) - 130.0) < 0.5
    # Symmetric about the center.
    assert abs((le[0] + he[0]) / 2 - 19.2556) < 1e-9
    assert abs((le[1] + he[1]) / 2 - 47.4369) < 1e-9


def test_rotation_normalized_minimal_turn():
    # Runway is a line: 130° needs only a −50° turn to stand vertical.
    assert rotation_for_heading(130.0) == abs(rotation_for_heading(130.0)) * -1
    assert abs(rotation_for_heading(130.0) - (-50.0)) < 1e-9
    assert abs(rotation_for_heading(310.0) - (-50.0)) < 1e-9
    assert abs(rotation_for_heading(0.0)) < 1e-9
    for heading in (10.0, 45.0, 90.0, 179.0, 250.0, 359.0):
        assert -90.0 <= rotation_for_heading(heading) <= 90.0


def test_single_rotation_puts_runway_vertical_and_moves_north():
    """The core spec invariant: one matrix aligns the runway AND the arrow."""
    le, he = (100.0, 0.0), (100.0 + math.sin(math.radians(130.0)) * 500,
                            math.cos(math.radians(130.0)) * 500)
    heading = runway_heading_deg(le, he)
    assert abs(heading - 130.0) < 1e-9
    rotation = rotation_for_heading(heading)
    rle = rotate_point(*le, rotation)
    rhe = rotate_point(*he, rotation)
    # Runway line is vertical after rotation.
    assert abs(rhe[0] - rle[0]) < 1e-6
    # North went with it by exactly the same rotation (never left pointing up).
    north = rotate_point(0.0, 1.0, rotation)
    assert abs(math.hypot(*north) - 1.0) < 1e-9
    assert abs(math.degrees(math.atan2(north[0], north[1])) - (-rotation)) < 1e-9


def test_primary_runway_is_longest():
    assert primary_runway_index([None, 3000.0, 2500.0]) == 1
    assert primary_runway_index([None, None]) == 0


def test_project_is_local_and_north_up():
    x, y = project(19.2556, 47.4369, 19.2556, 47.4369)
    assert (x, y) == (0.0, 0.0)
    _, north = project(19.2556, 47.4469, 19.2556, 47.4369)
    assert north > 1000.0  # ~1105 m per 0.01 deg lat
    east, _ = project(19.2656, 47.4369, 19.2556, 47.4369)
    assert east > 700.0


def test_query_builder_uses_around_and_geom():
    query = build_airport_query(47.4369, 19.2556, 3000.0)
    assert "around:3000,47.436900,19.255600" in query
    assert 'way["aeroway"]' in query
    assert 'relation["aeroway"]' in query
    assert "out geom;" in query


def _elements():
    return [
        {"type": "way", "id": 1, "tags": {"aeroway": "taxiway"},
         "geometry": [{"lon": 19.25, "lat": 47.43}, {"lon": 19.26, "lat": 47.43}]},
        {"type": "way", "id": 2, "tags": {"aeroway": "apron"},
         "geometry": [{"lon": 19.25, "lat": 47.43}, {"lon": 19.26, "lat": 47.43},
                      {"lon": 19.26, "lat": 47.44}, {"lon": 19.25, "lat": 47.44},
                      {"lon": 19.25, "lat": 47.43}]},
        {"type": "way", "id": 3, "tags": {"aeroway": "helipad"},
         "geometry": [{"lon": 19.25, "lat": 47.43}, {"lon": 19.26, "lat": 47.43}]},
        {"type": "way", "id": 4, "tags": {"highway": "service"},
         "geometry": [{"lon": 19.25, "lat": 47.43}, {"lon": 19.26, "lat": 47.43}]},
        {"type": "way", "id": 5, "tags": {"aeroway": "terminal"},
         "geometry": [{"lon": 19.25, "lat": 47.43}]},  # single point: dropped
    ]


def test_split_aeroway_classes_and_ignores_rest():
    geoms, counts = split_aeroway(_elements())
    assert set(geoms) == set(AEROWAY_CLASSES)
    assert len(geoms["taxiway"]) == 1
    assert len(geoms["apron"]) == 1
    assert geoms["runway"] == [] and geoms["hangar"] == []
    assert geoms["stands"] == [] and geoms["stopways"] == []
    assert counts == {"nodes": 0, "ways": 3, "relations": 0}  # helipad + untagged excluded


def _airport():
    return {
        "ident": "LHBP",
        "name": "Budapest Liszt Ferenc Intl",
        "latitude_deg": 47.4369,
        "longitude_deg": 19.2556,
        "elevation_ft": "495",
        "iata_code": "BUD",
        "iso_country": "HU",
        "municipality": "Budapest",
    }


def _runway_row():
    return {
        "le_ident": "13L", "he_ident": "31R",
        "length_ft": "3000", "width_ft": "150", "surface": "ASP",
        "le_latitude_deg": "47.4395", "le_longitude_deg": "19.2510",
        "he_latitude_deg": "47.4342", "he_longitude_deg": "19.2602",
        "le_displaced_threshold_ft": "200", "he_displaced_threshold_ft": "0",
    }


def test_render_blueprint_groups_and_badges():
    osm = {
        "runway": [[(19.251, 47.4395), (19.2602, 47.4342)]],
        "taxiway": [[(19.252, 47.438), (19.259, 47.435)]],
        "apron": [[(19.255, 47.437), (19.257, 47.437), (19.257, 47.436),
                   (19.255, 47.436), (19.255, 47.437)]],
        "terminal": [], "hangar": [],
    }
    freqs = [{"type": "TWR", "description": "Tower", "frequency_mhz": 118.1}]
    svg, counts, rotation, warnings = render_diagram(
        airport=_airport(), runways=[_runway_row()], frequencies=freqs,
        osm_geoms=osm, width=1000,
    )
    assert warnings == []
    # Primary 130° runway stands vertical via the minimal −50° turn.
    assert abs(rotation - rotation_for_heading(130.0)) < 5.0
    for marker in ("osm-taxiways", "osm-aprons",
                   "runways", "runway-marks", "compass"):
        assert f'id="{marker}"' in svg, marker
    # No top strip either: frequencies live in responses/page, never plot.
    assert 'id="freq-strip"' not in svg
    assert "118.100" not in svg and "TWR" not in svg
    # No footer: no elevation/credits plotted anywhere in the artwork.
    assert 'id="footer"' not in svg
    assert "Elev." not in svg
    assert "OurAirports" not in svg and "OpenStreetMap" not in svg
    # No in-SVG title: name/country live as fixed page labels, never plot.
    assert 'id="title"' not in svg
    assert "BUDAPEST" not in svg
    # All labels are crisp single-stroke (never raster-traced outlines).
    assert svg.count('data-stroke-font="hershey"') >= 5
    assert "13L" in svg and "31R" in svg  # ident badges
    assert "130°" in svg and "310°" in svg  # degree ovals
    # Geometry-bold: 1 OSM outline + 2 edges + 1 centerline per strip.
    assert counts == {"runway": 4, "taxiway": 1, "apron": 1,
                      "terminal": 0, "hangar": 0, "stands": 0,
                      "stopways": 0, "highways": 0, "roads": 0,
                      "paths": 0, "rails": 0, "waterway": 0,
                      "water": 0, "buildings": 0}
    # Strokes only: no fills except the structurally-needed arrowhead.
    assert svg.count('fill="#000000"') <= 10
    assert 'fill="none"' in svg


def test_split_folds_taxilane_stands_stopway():
    from backend.airports.overpass import build_context_query, split_context

    elements = [
        {"type": "way", "id": 1, "tags": {"aeroway": "taxilane"},
         "geometry": [{"lon": 19.25, "lat": 47.43}, {"lon": 19.26, "lat": 47.43}]},
        {"type": "way", "id": 2, "tags": {"aeroway": "parking_position"},
         "geometry": [{"lon": 19.25, "lat": 47.43}, {"lon": 19.251, "lat": 47.43},
                      {"lon": 19.251, "lat": 47.431}, {"lon": 19.25, "lat": 47.431},
                      {"lon": 19.25, "lat": 47.43}]},
        {"type": "node", "id": 3, "lon": 19.26, "lat": 47.43,
         "tags": {"aeroway": "parking_position"}},
        {"type": "way", "id": 4, "tags": {"aeroway": "stopway"},
         "geometry": [{"lon": 19.25, "lat": 47.43}, {"lon": 19.26, "lat": 47.43}]},
        {"type": "way", "id": 5, "tags": {"aeroway": "jet_bridge"},
         "geometry": [{"lon": 19.25, "lat": 47.43}, {"lon": 19.26, "lat": 47.43}]},
        {"type": "way", "id": 6, "tags": {"aeroway": "helipad"},
         "geometry": [{"lon": 19.25, "lat": 47.43}, {"lon": 19.26, "lat": 47.43}]},
    ]
    geoms, counts = split_aeroway(elements)
    assert len(geoms["taxiway"]) == 1  # taxilane folded in
    assert len(geoms["stands"]) == 2  # way ring + node diamond
    assert len(geoms["stopways"]) == 1
    # jet_bridge / helipad stay excluded (poster-scale clutter).
    assert sum(len(v) for v in geoms.values()) == 4
    assert counts == {"nodes": 1, "ways": 3, "relations": 0}  # excluded ids uncounted

    # Node diamond is a tiny closed ring around the node.
    diamond = geoms["stands"][1]
    assert len(diamond) == 5 and diamond[0] == diamond[-1]
    assert max(abs(x - 19.26) for x, _ in diamond) < 0.001

    # Context query shape + split (opt-in surrounding streets).
    query = build_context_query(47.4369, 19.2556, 3000.0)
    assert "around:3000,47.436900,19.255600" in query
    assert 'way["highway"' in query and 'way["building"]' in query
    assert 'way["natural"="water"]' in query and "out geom;" in query
    ctx = split_context([
        {"type": "way", "id": 10, "tags": {"highway": "service"},
         "geometry": [{"lon": 19.25, "lat": 47.43}, {"lon": 19.26, "lat": 47.43}]},
        {"type": "way", "id": 11, "tags": {"building": "yes"},
         "geometry": [{"lon": 19.25, "lat": 47.43}, {"lon": 19.26, "lat": 47.43},
                      {"lon": 19.26, "lat": 47.431}, {"lon": 19.25, "lat": 47.43}]},
        {"type": "way", "id": 12, "tags": {"shop": "bakery"},
         "geometry": [{"lon": 19.25, "lat": 47.43}, {"lon": 19.26, "lat": 47.43}]},
        {"type": "way", "id": 13, "tags": {"highway": "motorway"},
         "geometry": [{"lon": 19.25, "lat": 47.43}, {"lon": 19.26, "lat": 47.43}]},
        {"type": "way", "id": 14, "tags": {"highway": "footway"},
         "geometry": [{"lon": 19.25, "lat": 47.43}, {"lon": 19.26, "lat": 47.43}]},
        {"type": "way", "id": 15, "tags": {"railway": "rail"},
         "geometry": [{"lon": 19.25, "lat": 47.43}, {"lon": 19.26, "lat": 47.43}]},
        {"type": "way", "id": 16, "tags": {"waterway": "stream"},
         "geometry": [{"lon": 19.25, "lat": 47.43}, {"lon": 19.26, "lat": 47.43}]},
    ])
    assert len(ctx["roads"]) == 1 and len(ctx["buildings"]) == 1
    assert len(ctx["highways"]) == 1 and len(ctx["paths"]) == 1
    assert len(ctx["rails"]) == 1 and len(ctx["waterway"]) == 1
    assert ctx["water"] == []


def test_render_stands_stopways_and_context_groups():
    osm = {
        "runway": [], "taxiway": [], "apron": [], "terminal": [],
        "hangar": [],
        "stands": [[(19.255, 47.437), (19.256, 47.437), (19.256, 47.4368),
                    (19.255, 47.4368), (19.255, 47.437)]],
        "stopways": [[(19.251, 47.4395), (19.252, 47.4395),
                      (19.252, 47.4393), (19.251, 47.4393),
                      (19.251, 47.4395)]],
    }
    ctx = {
        "highways": [], "roads": [[(19.24, 47.43), (19.27, 47.43)]],
        "paths": [], "rails": [],
        "waterway": [[(19.245, 47.431), (19.265, 47.431)]],
        "buildings": [[(19.24, 47.432), (19.241, 47.432), (19.241, 47.4318),
                       (19.24, 47.4318), (19.24, 47.432)]],
        "water": [],
    }
    layers = ["stands", "stopways", "roads", "waterway", "buildings"]
    svg, counts, _, warnings = render_diagram(
        airport=_airport(), runways=[], frequencies=[],
        osm_geoms=osm, context_geoms=ctx, width=1000, layers=layers,
    )
    assert 'id="osm-stands"' in svg and 'id="osm-stopways"' in svg
    assert 'id="context-roads"' in svg and 'id="context-waterway"' in svg
    assert 'id="context-railways"' not in svg and 'id="context-rail"' not in svg
    assert counts["stands"] == 1 and counts["stopways"] == 1
    assert counts["roads"] == 1 and counts["waterway"] == 1
    assert counts["buildings"] == 1 and counts["runway"] == 0
    assert "no_context_data" not in warnings
    # Empty context warns instead of failing.
    _, _, _, warnings2 = render_diagram(
        airport=_airport(), runways=[], frequencies=[],
        osm_geoms={k: [] for k in osm}, context_geoms={}, width=1000,
        layers=layers,
    )
    assert "no_context_data" not in warnings2  # nothing requested, nothing given
    _, _, _, warnings3 = render_diagram(
        airport=_airport(), runways=[], frequencies=[],
        osm_geoms={k: [] for k in osm},
        context_geoms={k: [] for k in ctx}, width=1000, layers=layers,
    )
    assert "no_context_data" in warnings3


def test_render_layers_filter_draw_fit_and_counts():
    """layers=[taxiway]: only taxiways draw, count, and frame the fit."""
    osm = {
        "runway": [[(19.251, 47.4395), (19.2602, 47.4342)]],
        "taxiway": [[(19.252, 47.438), (19.259, 47.435)]],
        "apron": [[(19.255, 47.437), (19.257, 47.437), (19.257, 47.436),
                   (19.255, 47.436), (19.255, 47.437)]],
        "terminal": [], "hangar": [], "stands": [], "stopways": [],
    }
    svg, counts, _, _ = render_diagram(
        airport=_airport(), runways=[_runway_row()], frequencies=[],
        osm_geoms=osm, width=1000, layers=["taxiway"],
    )
    assert 'id="osm-taxiways"' in svg
    assert 'id="runways"' not in svg and 'id="runway-marks"' not in svg
    assert 'id="osm-runways"' not in svg and 'id="osm-aprons"' not in svg
    assert counts["taxiway"] == 1 and counts["runway"] == 0
    assert sum(counts.values()) == 1


def test_render_zoom_scales_about_center():
    """Zoom fills the page: geometry distances scale, labels don't."""
    import re

    osm = {"runway": [], "taxiway": [[(19.252, 47.438), (19.259, 47.435)]],
           "apron": [], "terminal": [], "hangar": []}

    def runway_len(svg):
        group = svg.split('id="runways"')[1].split("</g>")[0]
        x1, y1, x2, y2 = map(float, re.findall(
            r"M ([\d.]+) ([\d.]+) L ([\d.]+) ([\d.]+)", group)[0])
        import math as _math
        return _math.hypot(x2 - x1, y2 - y1)

    svg1, _, _, _ = render_diagram(
        airport=_airport(), runways=[_runway_row()], frequencies=[],
        osm_geoms=osm, width=1000, zoom=1.0)
    svg2, _, _, _ = render_diagram(
        airport=_airport(), runways=[_runway_row()], frequencies=[],
        osm_geoms=osm, width=1000, zoom=0.5)
    assert abs(runway_len(svg2) / runway_len(svg1) - 0.5) < 0.01
    # Same document height grows (page fills vertically too).
    h1 = float(re.search(r'height="([\d.]+)"', svg1).group(1))
    h2 = float(re.search(r'height="([\d.]+)"', svg2).group(1))
    assert h1 > h2


def test_geometry_independent_of_frequency_strip():
    """Regression: the strip loop once rebound the fit-center closure var,
    shifting all geometry out of the clipped frame whenever frequencies
    were present (all counts dropped to 0)."""
    osm = {"runway": [], "taxiway": [[(19.252, 47.438), (19.259, 47.435)]],
           "apron": [], "terminal": [], "hangar": []}
    freqs = [{"type": "TWR", "description": "Tower", "frequency_mhz": 118.1}]
    svg_bare, counts_bare, _, _ = render_diagram(
        airport=_airport(), runways=[_runway_row()], frequencies=[],
        osm_geoms=osm, width=1000)
    svg_full, counts_full, _, _ = render_diagram(
        airport=_airport(), runways=[_runway_row()], frequencies=freqs,
        osm_geoms=osm, width=1000)
    assert counts_full["taxiway"] == counts_bare["taxiway"] == 1
    assert counts_full["runway"] == counts_bare["runway"] == 3

    def group_paths(svg, gid):
        section = svg.split(f'id="{gid}"')[1].split("</g>")[0]
        import re
        return re.findall(r"<path d=\"[^\"]+\"/>", section)

    assert group_paths(svg_full, "osm-taxiways") == group_paths(svg_bare, "osm-taxiways")
    assert group_paths(svg_full, "runways") == group_paths(svg_bare, "runways")


def test_zoomed_content_is_cut_at_the_frame():
    """Zoomed geometry must not spill into the margins (or past them)."""
    import re

    osm = {"runway": [], "taxiway": [[(19.0, 47.43), (19.5, 47.43)]],
           "apron": [], "terminal": [], "hangar": []}
    svg, _, _, _ = render_diagram(
        airport=_airport(), runways=[_runway_row()], frequencies=[],
        osm_geoms=osm, width=1000, zoom=3.0)
    for gid in ("osm-taxiways", "runways", "runway-marks"):
        section = svg.split(f'id="{gid}"')[1].split("</g>")[0]
        xs = [float(x) for x, _ in re.findall(r"[ML] ([\d.]+) ([\d.]+)", section)]
        assert xs, gid
        assert min(xs) >= 39.9 and max(xs) <= 960.1, (gid, min(xs), max(xs))


def test_render_version_busts_stale_svg_cache():
    """The cache key tracks the renderer source hash: EVERY code change
    busts stale SVGs automatically (the old manual integer was forgotten
    once already — same params served a stale layout for 24h)."""
    import hashlib
    import os

    from backend.airports.render import render_source_version
    from backend.airports.router import render_diagram_version

    assert render_diagram_version() == render_source_version()
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "airports", "render.py"), "rb") as fh:
        expected = hashlib.sha1(fh.read()).hexdigest()[:12]
    assert render_source_version() == expected
    from backend.airports.cache import airports_cache_key

    # The version is mixed into the digest: any source change re-keys.
    keyed = airports_cache_key("svg", "LHBP", "r=3000", f"v={expected}")
    assert airports_cache_key("svg", "LHBP", "r=3000") != keyed
    assert airports_cache_key("svg", "LHBP", "r=3000", "v=deadbeef") != keyed


def test_render_request_zoom_defaults_and_rejects(http_client):
    from backend.airports.schemas import RenderRequest

    assert RenderRequest(icao="LHBP").zoom == 1.0
    resp = http_client.post(
        "/v1/airports/render", json={"icao": "LHBP", "zoom": 0})
    assert resp.status_code == 422
    resp = http_client.post(
        "/v1/airports/render", json={"icao": "LHBP", "zoom": 99})
    assert resp.status_code == 422


def test_render_endpoint_wires_layers_without_network():
    """Regression (F-004): a leftover body.context in the render branch
    500'd every live render — unit tests calling render_diagram directly
    never touch router wiring, so exercise it here with stubbed I/O."""
    import asyncio
    from unittest.mock import patch

    from starlette.requests import Request

    import backend.airports.router as router_mod
    from backend.airports.schemas import RenderRequest

    airport = {
        "ident": "LHBP", "name": "Budapest", "municipality": "Budapest",
        "iso_country": "HU", "latitude_deg": 47.4369, "longitude_deg": 19.2556,
    }

    async def fake_lookup(icao):
        return airport, [], [], [], False

    async def fake_polygons(lat, lon, radius_m, warnings, kind="overpass"):
        return [], None

    async def scenario():
        with (
            patch.object(router_mod, "_lookup", fake_lookup),
            patch.object(router_mod, "_load_polygons", fake_polygons),
        ):
            scope = {
                "type": "http", "headers": [], "query_string": b"",
                "server": ("testserver", 80), "scheme": "http", "path": "/",
            }
            return await router_mod.render(
                RenderRequest(icao="LHBP", layers=["taxiway"]), Request(scope))

    resp = asyncio.run(scenario())
    assert resp.icao == "LHBP"
    assert resp.rotation_deg == 0.0
    assert resp.path_counts["taxiway"] == 0
    assert "no_osm_aeroway" in resp.warnings


def test_render_request_layers_defaults_and_rejects(http_client):
    """Hermetic: airfield default, unknown/empty layers 422, no Upstreams."""
    from backend.airports.schemas import (
        AIRFIELD_LAYERS,
        CONTEXT_LAYERS,
        RenderRequest,
    )

    req = RenderRequest(icao="LHBP")
    assert req.layers is None
    assert req.effective_layers() == list(AIRFIELD_LAYERS)
    assert req.needs_context() is False
    assert RenderRequest(icao="LHBP", layers=["roads", "roads"]).layers == ["roads"]
    assert RenderRequest(
        icao="LHBP", layers=["roads"]).needs_context() is True
    assert set(AIRFIELD_LAYERS) | set(CONTEXT_LAYERS) == {
        "runway", "taxiway", "apron", "terminal", "hangar", "stands",
        "stopways", "highways", "roads", "paths", "rails", "waterway",
        "water", "buildings",
    }
    resp = http_client.post(
        "/v1/airports/render", json={"icao": "LHBP", "layers": ["motorways"]})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"
    resp = http_client.post(
        "/v1/airports/render", json={"icao": "LHBP", "layers": []})
    assert resp.status_code == 422
    # The old F-002 flag is gone (layers subsume it).
    resp = http_client.post(
        "/v1/airports/render", json={"icao": "LHBP", "context": True})
    assert resp.status_code == 422


def test_render_runway_edges_are_parallel_at_true_width():
    """Boldness is geometry (±width/2 edge paths), so it survives the
    vector convert pipeline that discards stroke-width."""
    import re

    osm = {"runway": [], "taxiway": [], "apron": [],
           "terminal": [], "hangar": []}
    svg, counts, _, _ = render_diagram(
        airport=_airport(), runways=[_runway_row()], frequencies=[],
        osm_geoms=osm, width=1000,
    )
    assert counts["runway"] == 3  # 2 edges + 1 centerline, no OSM outlines
    group = svg.split('id="runways"')[1].split("</g>")[0]
    segs = re.findall(r"M ([\d.]+) ([\d.]+) L ([\d.]+) ([\d.]+)", group)
    assert len(segs) == 3
    (x1, y1, x2, y2), (a1, b1, a2, b2), (c1, d1, c2, d2) = (
        tuple(map(float, s)) for s in segs)
    # Edges are parallel to the centerline and symmetric about it.
    assert abs((x1 + a1) / 2 - c1) < 0.05 and abs((y1 + b1) / 2 - d1) < 0.05
    edge_gap = math.hypot(x1 - a1, y1 - b1)
    assert edge_gap > 0  # true scaled width apart, not a triple-drawn line


def test_render_no_runway_data_warns_but_draws_osm():
    svg, counts, rotation, warnings = render_diagram(
        airport=_airport(), runways=[], frequencies=[],
        osm_geoms={"runway": [], "taxiway": [[(19.252, 47.438), (19.259, 47.435)]],
                   "apron": [], "terminal": [], "hangar": []},
        width=1000,
    )
    assert rotation == 0.0
    assert "no_runway_data" in warnings
    assert counts["taxiway"] == 1
    # Frequencies never plot (no strip, no warning either way).
    assert "no_frequencies" not in warnings
    assert "frequency" not in svg.lower()


def test_render_svg_parses_as_plottable_vectors():
    osm = {"runway": [], "taxiway": [[(19.252, 47.438), (19.259, 47.435)]],
           "apron": [], "terminal": [], "hangar": []}
    svg, _, _, _ = render_diagram(
        airport=_airport(), runways=[_runway_row()], frequencies=[],
        osm_geoms=osm, width=1000,
    )
    polys, width, height, _ = imaging.parse_svg_vectors(svg.encode("utf-8"))
    assert len(polys) > 0
    assert width > 0 and height > 0


def test_cache_key_deterministic_and_memory_roundtrip():
    configure_airports_redis(None)  # force the in-process fallback
    assert airports_cache_key("svg", "a", "b") == airports_cache_key("svg", "a", "b")
    assert airports_cache_key("svg", "a") != airports_cache_key("raw", "a")

    async def roundtrip():
        assert await cache_get("fund-vista:airports:v1:test:x") is None
        assert await cache_set("fund-vista:airports:v1:test:x", "hello", 60) is True
        assert await cache_get("fund-vista:airports:v1:test:x") == "hello"

    asyncio.run(roundtrip())


def test_lookup_rejects_malformed_icao_without_network(http_client):
    """Hermetic: 422 comes from validation, OurAirports is never touched."""
    resp = http_client.get("/v1/airports/lookup?icao=TOOLONG")
    assert resp.status_code == 422
    resp = http_client.get("/v1/airports/lookup?icao=12!")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


def test_render_rejects_bad_body_without_network(http_client):
    resp = http_client.post("/v1/airports/render", json={"icao": "XX!"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"
    resp = http_client.post("/v1/airports/render", json={})
    assert resp.status_code == 422
    resp = http_client.post(
        "/v1/airports/render", json={"icao": "LHBP", "radius_m": 100})
    assert resp.status_code == 422


def test_results_unknown_token_404_without_network(http_client):
    resp = http_client.get("/v1/airports/results/" + "0" * 40)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "result_not_found"
    resp = http_client.get("/v1/airports/results/not-a-token")
    assert resp.status_code == 404


def test_airports_page_renders_search_and_convert_sections(http_client):
    resp = http_client.get("/airports")
    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers["content-type"]
    html = resp.text
    assert 'id="icao_code"' in html
    assert 'id="icaoLookupBtn"' in html
    assert "/v1/airports/lookup" in html
    assert "/v1/airports/render" in html
    assert "/v1/airports/import" in html
    # Shared convert sections (Page & pen, Label, Display, stats, download).
    for marker in (
        'id="page_size"', 'id="label_enabled"', 'id="label_text"',
        'id="background"', 'id="preview"', 'id="stats"', 'id="vpype"',
        "/v1/convert",
    ):
        assert marker in html, marker
    # Airport search picker (F-003).
    assert 'id="apt_search"' in html
    assert 'id="aptSearchBtn"' in html
    assert 'id="apt_candidate"' in html
    assert "/v1/airports/search" in html
    # Single flow: every control auto-renders (no manual preview button,
    # no second raw-SVG preview — the convert panel is the only preview).
    assert 'id="aptBtn"' in html
    assert 'id="aptPreviewBtn"' not in html
    assert 'id="diagram_preview"' not in html
    assert "scheduleRender" in html
    assert "lastPlottedIcao" in html
    # Zoom slider to fill the A4 page.
    assert 'id="apt_zoom"' in html
    # Layer checkboxes (airfield ticked, context unticked by default).
    for layer in ("runway", "taxiway", "apron", "stands", "highways",
                  "roads", "water", "buildings", "rails"):
        assert f'value="{layer}"' in html, layer
    assert 'id="apt_context"' not in html
    # Fixed top labels (name/country live on the page, not in the SVG).
    assert 'id="apt_title"' in html
    assert "setAirportTitle" in html


def test_airports_page_negative(http_client):
    """Unknown /airports sub-paths must not 5xx."""
    resp = http_client.get("/airports/__nope__")
    assert resp.status_code == 404


def _search_rows():
    return [
        {"ident": "LHBP", "gps_code": "LHBP", "iata_code": "BUD",
         "name": "Budapest Liszt Ferenc International Airport",
         "municipality": "Budapest", "iso_country": "HU",
         "type": "large_airport", "latitude_deg": "47.43", "longitude_deg": "19.26"},
        {"ident": "LHDC", "gps_code": "", "iata_code": "",
         "name": "Bekescsaba Airport", "municipality": "Bekescsaba",
         "iso_country": "HU", "type": "small_airport",
         "latitude_deg": "46.6", "longitude_deg": "21.0"},
        {"ident": "LHXX", "gps_code": "", "iata_code": "",
         "name": "Old Budapest Field", "municipality": "Budapest",
         "iso_country": "HU", "type": "closed",
         "latitude_deg": "47.0", "longitude_deg": "19.0"},
    ]


def test_rank_exact_iata_beats_name_substring():
    from backend.airports.ourairports import rank_candidates

    top = rank_candidates(_search_rows(), "BUD")
    assert [c["icao"] for c in top] == ["LHBP"]
    assert top[0]["iata"] == "BUD"


def test_rank_prefix_then_size_then_limit():
    from backend.airports.ourairports import rank_candidates

    ordered = rank_candidates(_search_rows(), "LH")
    assert [c["icao"] for c in ordered] == ["LHBP", "LHDC"]  # large first
    assert [c["icao"] for c in rank_candidates(_search_rows(), "LH", limit=1)] == ["LHBP"]
    # Closed rows never surface, even on name match.
    assert all(c["icao"] != "LHXX" for c in rank_candidates(_search_rows(), "budapest"))
    assert rank_candidates(_search_rows(), "xyz") == []
    assert rank_candidates(_search_rows(), "  ") == []


def test_search_validation_without_network(http_client):
    """Hermetic: 422 comes from validation, OurAirports is never touched."""
    resp = http_client.get("/v1/airports/search?q=x")
    assert resp.status_code == 422
    resp = http_client.get("/v1/airports/search")
    assert resp.status_code == 422
    resp = http_client.get("/v1/airports/search?q=Budapest&limit=99")
    assert resp.status_code == 422
    resp = http_client.get("/v1/airports/search?q=Budapest&limit=0")
    assert resp.status_code == 422
