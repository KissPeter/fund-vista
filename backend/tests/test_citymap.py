"""Citymap tests: chrome stripping, layers, query/render units (no network).

All cases are hermetic — Overpass/Nominatim are never touched (query shape
is asserted as a string, splitting/rendering run on synthetic elements).
The only HTTP cases hit local metadata/validation endpoints.
"""

from __future__ import annotations

import asyncio

from backend.citymap.cache import (
    cache_get,
    cache_set,
    citymap_cache_key,
    configure_citymap_redis,
)
from backend.citymap.chrome import strip_city_roads_chrome
from backend.citymap.layers import LAYERS, LAYER_ORDER
from backend.citymap.overpass import (
    bbox_str,
    build_overpass_query,
    match_relation_layer,
    match_way_layer,
    split_elements,
)
from backend.citymap.render import render_svg
from backend.penplot import imaging
from backend.penplot.store import ImageStore

CITY_ROADS_SVG = """<!-- Generator: https://github.com/anvaka/city-roads
Data © OpenStreetMap contributors, ODbL 1.0. https://osm.org/copyright
--><svg xmlns="http://www.w3.org/2000/svg" width="800" height="600" viewBox="0 0 800 600"><g fill="none" stroke="black"><path d="M 10 10 L 20 20"/></g><text text-anchor="end" x="790" y="595" fill="black" font-family="sans-serif" font-size="24">Budapest</text><text text-anchor="end" x="790" y="570" fill="black" font-family="sans-serif" font-size="12">data © OpenStreetMap</text></svg>"""  # noqa: E501


def test_chrome_strip_removes_caption_and_credit():
    cleaned, removed = strip_city_roads_chrome(CITY_ROADS_SVG)
    assert "<path" in cleaned
    assert "<text" not in cleaned
    assert "Budapest" not in cleaned
    assert "OpenStreetMap" not in cleaned
    assert len(removed) == 3  # 2 captions + 1 generator comment


def test_chrome_strip_is_idempotent():
    cleaned, _ = strip_city_roads_chrome(CITY_ROADS_SVG)
    cleaned_again, removed_again = strip_city_roads_chrome(cleaned)
    assert removed_again == []
    assert cleaned_again == cleaned


def test_chrome_strip_keeps_geometry_groups():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<g id="citymap-roads"><path d="M 0 0 L 1 1"/></g></svg>'
    )
    cleaned, removed = strip_city_roads_chrome(svg)
    assert removed == []
    assert 'id="citymap-roads"' in cleaned


def test_layer_registry_covers_drawscape_set():
    for layer in ("highways", "roads", "rails", "water", "waterway",
                  "buildings", "aeroway", "ferry"):
        assert layer in LAYERS, layer
        assert LAYERS[layer]["label"]
        assert LAYERS[layer]["description"]
    assert set(LAYER_ORDER) == set(LAYERS)


def test_query_builder_contains_layers_and_bbox():
    bbox = (47.4, 19.0, 47.6, 19.3)
    query = build_overpass_query(bbox, ["highways", "ferry"])
    assert bbox_str(bbox) in query
    assert 'way["highway"~"^(motorway|trunk|primary)(_link)?$"]' in query
    assert 'relation["route"="ferry"]' in query
    assert "out skel qt;" in query
    # Every union member must be semicolon-terminated or Overpass 400/406s.
    assert f"]({bbox_str(bbox)});" in query


def _elements():
    return [
        {"type": "node", "id": 1, "lon": 19.0, "lat": 47.5},
        {"type": "node", "id": 2, "lon": 19.1, "lat": 47.5},
        {"type": "node", "id": 3, "lon": 19.1, "lat": 47.6},
        {"type": "node", "id": 4, "lon": 19.0, "lat": 47.6},
        {"type": "way", "id": 10, "nodes": [1, 2, 3],
         "tags": {"highway": "motorway"}},
        {"type": "way", "id": 11, "nodes": [1, 4],
         "tags": {"highway": "residential"}},
        {"type": "way", "id": 12, "nodes": [1, 2, 3, 4, 1],
         "tags": {"building": "yes"}},
        {"type": "way", "id": 13, "nodes": [2, 3],
         "tags": {"highway": "primary"}},
        {"type": "relation", "id": 20,
         "tags": {"route": "ferry"},
         "members": [{"type": "way", "ref": 13, "role": ""}]},
    ]


def test_split_assigns_first_match_and_claims_relation_members():
    layers = ["highways", "roads", "buildings", "ferry"]
    geoms, counts = split_elements(_elements(), layers)
    # Ferry member way 13 is claimed by the ferry relation, not highways.
    assert len(geoms["ferry"]) == 1
    assert len(geoms["highways"]) == 1  # way 10 only
    assert len(geoms["roads"]) == 1  # way 11
    assert len(geoms["buildings"]) == 1  # closed ring way 12
    assert counts == {"nodes": 4, "ways": 4, "relations": 1}


def test_match_helpers():
    assert match_way_layer({"highway": "motorway_link"}, ["highways", "roads"]) == "highways"
    assert match_way_layer({"highway": "cycleway"}, ["roads", "paths"]) == "paths"
    assert match_way_layer({"railway": "tram"}, ["rails"]) == "rails"
    assert match_way_layer({"natural": "water"}, ["water", "buildings"]) == "water"
    assert match_way_layer({"building": "yes"}, ["buildings"]) == "buildings"
    assert match_way_layer({"shop": "bakery"}, ["roads", "buildings"]) is None
    assert match_relation_layer({"route": "ferry"}, ["ferry"]) == "ferry"


def test_render_groups_per_layer_no_chrome():
    layers = ["highways", "roads", "buildings"]
    geoms, _ = split_elements(_elements(), layers + ["ferry"])
    bbox = (47.4, 19.0, 47.6, 19.3)
    svg, counts = render_svg(geoms, bbox, layers, width=1000)
    for layer in layers:
        assert f'id="citymap-{layer}"' in svg
    assert 'id="citymap-ferry"' not in svg
    assert "<text" not in svg
    assert "<!--" not in svg
    assert counts == {"highways": 1, "roads": 1, "buildings": 1}
    # Closed building ring is outlined with Z.
    assert " Z" in svg


def test_render_min_path_length_drops_short_paths():
    layers = ["roads"]
    geoms, _ = split_elements(_elements(), layers)
    bbox = (47.4, 19.0, 47.6, 19.3)
    _, counts_all = render_svg(geoms, bbox, layers)
    assert counts_all["roads"] == 1
    _, counts_filtered = render_svg(geoms, bbox, layers, min_path_len_m=1e9)
    assert counts_filtered["roads"] == 0


def test_layers_endpoint_lists_registry(http_client):
    resp = http_client.get("/v1/citymap/layers")
    assert resp.status_code == 200, resp.text
    ids = [layer["id"] for layer in resp.json()["layers"]]
    assert ids == list(LAYER_ORDER)


def test_render_rejects_unknown_layer_422(http_client):
    resp = http_client.post(
        "/v1/citymap/render",
        json={"city": "Budapest", "layers": ["motorways"]},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


def test_render_needs_exactly_one_source_422(http_client):
    both = {
        "city": "Budapest",
        "bbox": {"south": 47.4, "west": 19.0, "north": 47.6, "east": 19.3},
        "layers": ["roads"],
    }
    resp = http_client.post("/v1/citymap/render", json=both)
    assert resp.status_code == 422
    neither = {"layers": ["roads"]}
    resp = http_client.post("/v1/citymap/render", json=neither)
    assert resp.status_code == 422


def test_cache_key_deterministic_and_memory_roundtrip():
    configure_citymap_redis(None)  # force the in-process fallback
    assert citymap_cache_key("svg", "a", "b") == citymap_cache_key("svg", "a", "b")
    assert citymap_cache_key("svg", "a") != citymap_cache_key("raw", "a")

    async def roundtrip():
        assert await cache_get("fund-vista:citymap:v1:test:x") is None
        assert await cache_set("fund-vista:citymap:v1:test:x", "hello", 60) is True
        assert await cache_get("fund-vista:citymap:v1:test:x") == "hello"

    asyncio.run(roundtrip())


def test_geocode_search_rejects_limit_over_10(http_client):
    """CR-001 Phase 3: limit bounds are validated before any Nominatim call
    (hermetic — 422 comes from validation, no network)."""
    resp = http_client.get("/v1/citymap/geocode/search?city=Budapest&limit=99")
    assert resp.status_code == 422
    resp = http_client.get("/v1/citymap/geocode/search?city=Budapest&limit=0")
    assert resp.status_code == 422


def test_citymap_page_renders_map_section(http_client):
    """CR-001 Phase 2: standalone /citymap page with searchable, pannable,
    zoomable preview plus the shared convert sections."""
    resp = http_client.get("/citymap")
    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers["content-type"]
    html = resp.text
    # MapLibre preview on free OpenFreeMap tiles.
    assert 'id="map"' in html
    assert "maplibre" in html.lower()
    assert "tiles.openfreemap.org" in html
    # Search -> candidate picker -> layer filters.
    assert 'id="city_name"' in html
    assert 'id="citySearchBtn"' in html
    assert 'id="city_candidate"' in html
    assert "/v1/citymap/geocode/search" in html
    for layer in ("highways", "roads", "ferry", "buildings"):
        assert f'value="{layer}"' in html, layer
    assert "/v1/citymap/import" in html
    # Shared convert sections (Page & pen, Label, Display, stats, download).
    for marker in (
        'id="page_size"', 'id="label_enabled"', 'id="label_text"',
        'id="background"', 'id="preview"', 'id="stats"', 'id="vpype"',
        "/v1/convert",
    ):
        assert marker in html, marker


def test_citymap_page_negative(http_client):
    """Unknown /citymap sub-paths must not 5xx."""
    resp = http_client.get("/citymap/__nope__")
    assert resp.status_code == 404


def test_import_rejects_unknown_layer_422(http_client):
    resp = http_client.post(
        "/v1/citymap/import",
        json={"city": "Budapest", "layers": ["motorways"]},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


def test_import_needs_exactly_one_source_422(http_client):
    resp = http_client.post(
        "/v1/citymap/import",
        json={
            "city": "Budapest",
            "bbox": {"south": 47.4, "west": 19.0, "north": 47.6, "east": 19.3},
            "layers": ["roads"],
        },
    )
    assert resp.status_code == 422
    resp = http_client.post("/v1/citymap/import", json={"layers": ["roads"]})
    assert resp.status_code == 422


def test_import_svg_roundtrip_through_image_store(tmp_path):
    """What /import stores must parse back as plottable vectors."""
    layers = ["highways", "roads"]
    geoms, _ = split_elements(_elements(), layers)
    bbox = (47.4, 19.0, 47.6, 19.3)
    svg_text, counts = render_svg(geoms, bbox, layers, width=1000)
    assert sum(counts.values()) > 0
    store = ImageStore(
        images_dir=str(tmp_path / "images"), results_dir=str(tmp_path / "results")
    )
    image_id = store.put_image_bytes(svg_text.encode("utf-8"), "svg")
    assert len(image_id) == 64
    path = store.find_image(image_id)
    assert path is not None and path.endswith(".svg")
    with open(path, "rb") as fh:
        raw = fh.read()
    polys, width, height, warnings = imaging.parse_svg_vectors(raw)
    assert len(polys) > 0
    assert width > 0 and height > 0


_OSM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="47.5" lon="19.0"/>
  <node id="2" lat="47.5" lon="19.1"/>
  <node id="3" lat="47.6" lon="19.1"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/>
    <tag k="highway" v="motorway"/>
  </way>
  <relation id="20">
    <member type="way" ref="10" role=""/>
    <tag k="route" v="ferry"/>
  </relation>
</osm>"""


def test_osm_xml_parses_to_overpass_shaped_elements():
    from backend.citymap.osm_api import parse_osm_xml

    elements = parse_osm_xml(_OSM_XML)
    kinds = {(el["type"], el["id"]) for el in elements}
    assert ("node", 1) in kinds
    assert ("way", 10) in kinds
    assert ("relation", 20) in kinds
    # Same split pipeline as Overpass payloads: ferry relation claims way 10.
    geoms, counts = split_elements(elements, ["highways", "ferry"])
    assert len(geoms["ferry"]) == 1
    assert len(geoms["highways"]) == 0
    assert counts == {"nodes": 3, "ways": 1, "relations": 1}


def test_osm_xml_rejects_garbage():
    from backend.citymap.osm_api import OsmApiError, parse_osm_xml

    try:
        parse_osm_xml("not xml at all <")
    except OsmApiError:
        pass
    else:
        raise AssertionError("expected OsmApiError")


def test_chunk_bbox_covers_large_area_with_small_cells():
    from backend.citymap.osm_api import chunk_bbox

    cells = chunk_bbox((47.0, 19.0, 47.8, 19.8), 0.25)
    assert len(cells) == 16  # 4x4 grid for a 0.8x0.8 bbox
    for south, west, north, east in cells:
        assert north - south <= 0.25 + 1e-9
        assert east - west <= 0.25 + 1e-9
    assert chunk_bbox((47.4, 19.0, 47.5, 19.1), 0.25) == [
        (47.4, 19.0, 47.5, 19.1)
    ]


def test_merge_elements_dedups_overlapping_cells():
    from backend.citymap.osm_api import merge_elements

    a = [{"type": "node", "id": 1}, {"type": "way", "id": 10}]
    b = [{"type": "node", "id": 1}, {"type": "node", "id": 2}]
    assert merge_elements([a, b]) == [
        {"type": "node", "id": 1},
        {"type": "way", "id": 10},
        {"type": "node", "id": 2},
    ]


def test_load_raw_falls_back_to_osm_api_on_overpass_outage():
    import backend.citymap.router as router_mod
    from backend.citymap.overpass import OverpassError

    async def scenario():
        async def boom(_query, _client=None):
            raise OverpassError("mirror1 504; mirror2 504")

        async def fallback(_bbox, _client=None):
            return list(_elements())

        orig_overpass, orig_osm = router_mod.fetch_overpass, router_mod.fetch_osm_api
        router_mod.fetch_overpass, router_mod.fetch_osm_api = boom, fallback
        try:
            warnings: list[str] = []
            # The bbox must contain the fixture's nodes (lat 47.5-47.6,
            # lon 19.0-19.1): _load_raw now clips the upstream payload to
            # the requested area, so a bbox elsewhere correctly yields
            # nothing and would not exercise the fallback at all.
            elements, err = await router_mod._load_raw(
                (47.45, 18.95, 47.65, 19.15), ["highways"], warnings
            )
        finally:
            router_mod.fetch_overpass, router_mod.fetch_osm_api = (
                orig_overpass,
                orig_osm,
            )
        return elements, err, warnings

    elements, err, warnings = asyncio.run(scenario())
    assert err is None
    assert elements is not None and len(elements) > 0
    assert "osm_api_fallback" in warnings
    # The fallback payload is layer-filtered on the way into the tile cache:
    # the motorway (10) and the primary (13) are both "highways", while the
    # residential (11) and the building (12) are dropped.
    assert {e["id"] for e in elements if e["type"] == "way"} == {10, 13}


def test_load_raw_502_when_both_upstreams_fail():
    import backend.citymap.router as router_mod
    from backend.citymap.osm_api import OsmApiError
    from backend.citymap.overpass import OverpassError

    async def scenario():
        async def boom(_query, _client=None):
            raise OverpassError("all mirrors 504")

        async def bust(_bbox, _client=None):
            raise OsmApiError("osm api 509")

        orig_overpass, orig_osm = router_mod.fetch_overpass, router_mod.fetch_osm_api
        router_mod.fetch_overpass, router_mod.fetch_osm_api = boom, bust
        try:
            warnings: list[str] = []
            elements, err = await router_mod._load_raw(
                (47.5, 19.1, 47.55, 19.15), ["highways"], warnings
            )
        finally:
            router_mod.fetch_overpass, router_mod.fetch_osm_api = (
                orig_overpass,
                orig_osm,
            )
        return elements, err

    elements, err = asyncio.run(scenario())
    assert elements is None
    assert err is not None
    assert err.status_code == 502
    assert "osm api 509" in err.body.decode().lower()
