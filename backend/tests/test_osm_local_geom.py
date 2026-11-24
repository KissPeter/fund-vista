"""Airports, airport traffic and the traffic basemap use the same extract (``around`` / bbox + ``out geom``)."""

from __future__ import annotations

import asyncio

import pytest

from backend.airports import overpass as ap
from backend.airports.sources import build_reduced_airport_query
from backend.citymap.overpass import build_overpass_query
from backend.osm_local import coverage as cov
from backend.osm_local import ql
from backend.osm_local.selectors import (
    AROUND_FILTERS,
    BBOX_FILTERS,
    registry,
    selector_mask,
    tags_match,
)
from backend.osm_local.store import Store, Writer
from backend.traffic import basemap as bm

SQUARE = "sq\n1\n 0.0 0.0\n 10.0 0.0\n 10.0 10.0\n 0.0 10.0\nEND\nEND\n"
# ~1.11 km per 0.01 deg lat at lat 5; the airport centre is (5.0, 5.0).
LAT, LON = 5.0, 5.0


def _seed(path, sels=None):
    sels = sels or registry()
    w = Writer(path)
    w.sels = sels  # what the database records as its registry

    def way(wid, tags, pts, keep=False):
        w.add_way(wid, selector_mask("way", tags, sels), tags,
                  [wid * 10 + i for i in range(len(pts))], pts, keep_bounds=keep)

    way(1, {"aeroway": "runway"}, [(5.0, 4.99), (5.0, 5.01)])               # through the centre
    way(2, {"aeroway": "hangar"}, [(5.001, 5.001), (5.001, 5.002), (5.002, 5.002), (5.001, 5.001)])
    way(3, {"aeroway": "runway"}, [(8.0, 8.0), (8.0, 8.1)])                  # far away
    way(4, {"highway": "residential"}, [(5.0, 5.02), (5.01, 5.02)])          # ~2.2 km out
    way(5, {"highway": "construction"}, [(5.0, 5.0), (5.0, 5.001)])          # not stored
    way(6, {"building": "yes"}, [(5.0, 5.003), (5.0, 5.004), (5.001, 5.004), (5.0, 5.003)])
    way(7, {}, [(5.003, 5.003), (5.003, 5.004), (5.004, 5.004), (5.003, 5.003)], keep=True)
    rel = {"type": "multipolygon", "aeroway": "apron"}
    w.add_relation(900, selector_mask("relation", rel, sels), rel, [(7, "outer")])
    # Traffic basemap features, far from the airport centre (lat/lon 5.0).
    way(10, {"highway": "motorway"}, [(7.0, 1.0), (7.0, 3.0)])
    way(11, {"highway": "primary"}, [(7.1, 1.0), (7.1, 3.0)])          # wider stored bit
    way(12, {"highway": "trunk"}, [(8.0, 1.0), (8.0, 3.0)])            # outside a small bbox
    way(13, {"waterway": "river", "name": "Duna"}, [(7.3, 1.0), (7.3, 3.0)])
    way(14, {"waterway": "stream", "name": "X"}, [(7.4, 1.0), (7.4, 3.0)])
    way(15, {"waterway": "river"}, [(7.5, 1.0), (7.5, 3.0)])           # unnamed
    way(16, {"natural": "water", "name": "Lake", "water": "lake"},
        [(7.6, 1.0), (7.6, 1.2), (7.7, 1.2), (7.6, 1.0)])
    way(17, {"natural": "water", "water": "lake"},
        [(7.8, 1.0), (7.8, 1.2), (7.9, 1.2), (7.8, 1.0)])              # unnamed
    way(18, {"natural": "water", "name": "Pond", "water": "pond"},
        [(7.8, 2.0), (7.8, 2.2), (7.9, 2.2), (7.8, 2.0)])              # not lake/reservoir
    way(19, {}, [(7.95, 1.0), (7.95, 1.2), (7.99, 1.2), (7.95, 1.0)], keep=True)
    way(20, {"highway": "motorway"}, [(12.0, 1.0), (12.0, 3.0)])  # outside coverage rectangle
    water_rel = {"type": "multipolygon", "natural": "water", "name": "Reservoir", "water": "reservoir"}
    w.add_relation(901, selector_mask("relation", water_rel, sels), water_rel, [(19, "outer")])
    w.finish(cov.parse_poly(SQUARE), "2026-11-16T00:00:00Z", "test.pbf")


@pytest.fixture
def local(tmp_path, monkeypatch):
    from backend.citymap.config import settings
    from backend.osm_local import service

    db = tmp_path / "osm.sqlite"
    _seed(db)
    monkeypatch.setattr(settings, "local_osm_db", str(db))
    service.reset()
    yield Store(db)
    service.reset()


def _ids(elements, kind):
    return {e["id"] for e in elements if e["type"] == kind}


def test_the_airport_queries_parse_and_every_filter_has_a_source():
    for q in (ap.build_airport_query(LAT, LON, 3000), ap.build_context_query(LAT, LON, 3000),
              build_reduced_airport_query(LAT, LON, 3000)):
        members = ql.parse_around(q)
        assert members
        for m in members:
            assert (m.kind, m.filter) in AROUND_FILTERS, m
    reg = set(registry())
    assert all(src in reg for srcs in AROUND_FILTERS.values() for src in srcs)


def test_the_citymap_parser_still_rejects_around_and_vice_versa():
    with pytest.raises(ql.QueryError):
        ql.parse(ap.build_airport_query(LAT, LON, 3000))
    with pytest.raises(ql.QueryError):
        ql.parse_around("[out:json][timeout:25];(way[\"a\"](0,0,1,1););out body;>;out skel qt;")


def test_airport_query_returns_out_geom_ways_and_relations(local):
    r = local.run_around(ql.parse_around(ap.build_airport_query(LAT, LON, 3000)))
    assert _ids(r["elements"], "way") == {1, 2}              # runway + hangar, not the far runway
    assert _ids(r["elements"], "relation") == {900}
    rel = next(e for e in r["elements"] if e["type"] == "relation")
    assert rel["members"][0]["geometry"][0] == {"lat": 5.003, "lon": 5.003}
    geoms, counts = ap.split_aeroway(r["elements"])
    assert len(geoms["runway"]) == 1 and len(geoms["hangar"]) == 1
    assert counts["ways"] == 2 and counts["relations"] == 1


def test_reduced_query_is_exact_on_the_narrower_regex(local):
    r = local.run_around(ql.parse_around(build_reduced_airport_query(LAT, LON, 3000)))
    assert _ids(r["elements"], "way") == {2}                 # hangar only
    assert all(e["tags"]["aeroway"] != "runway" for e in r["elements"] if e["type"] == "way")


def test_radius_is_a_distance_not_a_box(local):
    # Way 4 sits ~2.2 km east: out at 1.5 km, in at 3 km.
    near = local.run_around(ql.parse_around(ap.build_context_query(LAT, LON, 1500)))
    far = local.run_around(ql.parse_around(ap.build_context_query(LAT, LON, 3000)))
    assert 4 not in _ids(near["elements"], "way") and 4 in _ids(far["elements"], "way")


def test_context_splitter_sees_what_it_would_from_a_mirror(local):
    r = local.run_around(ql.parse_around(ap.build_context_query(LAT, LON, 3000)))
    assert 5 not in _ids(r["elements"], "way")               # highway=construction: not drawn anyway
    split = ap.split_context(r["elements"])
    assert len(split["roads"]) == 1 and len(split["buildings"]) == 1


def test_every_class_the_airport_splitters_draw_is_stored_locally():
    """The guard behind the wider ``["highway"]``-style filters: nothing they would draw is unstored."""
    sels = registry()
    samples = [
        {"highway": v} for v in (
            "motorway", "trunk", "primary", "motorway_link", "secondary", "tertiary", "unclassified",
            "residential", "living_street", "pedestrian", "service", "track", "road", "secondary_link",
            "footway", "path", "cycleway", "steps", "bridleway", "corridor")
    ] + [{"railway": v} for v in ("rail", "light_rail", "subway", "tram", "narrow_gauge", "monorail", "preserved")] + [
        {"waterway": v} for v in ("river", "stream", "canal", "ditch", "drain")
    ] + [{"natural": "water"}, {"water": "lake"}, {"building": "yes"}]
    for tags in samples:
        assert ap._match_context(tags) is not None, tags
        assert selector_mask("way", tags, sels) != 0, f"context class not stored: {tags}"
    for raw in ap.AEROWAY_CLASSES:
        assert selector_mask("way", {"aeroway": raw}, sels) != 0


def test_fetch_airport_polygons_prefers_local_then_mirrors(local, monkeypatch):
    got = asyncio.run(ap.fetch_airport_polygons(ap.build_airport_query(LAT, LON, 3000)))
    assert _ids(got, "way") == {1, 2}

    async def boom(*a, **k):
        raise AssertionError("mirror must not be called")
    monkeypatch.setattr(ap.OverpassClient, "fetch", boom)
    asyncio.run(ap.fetch_airport_polygons(ap.build_airport_query(LAT, LON, 3000)))  # still local

    called = []

    async def mirror(self, query):
        called.append(query)
        return [{"type": "way", "id": 99}]
    monkeypatch.setattr(ap.OverpassClient, "fetch", mirror)
    # outside the extract polygon -> mirrors
    out = asyncio.run(ap.fetch_airport_polygons(ap.build_airport_query(50.0, 50.0, 3000)))
    assert out == [{"type": "way", "id": 99}] and called
    # a query shape the local extract does not know -> mirrors
    called.clear()
    asyncio.run(ap.fetch_airport_polygons('[out:json][timeout:90];\nnode["highway"="motorway"](1,2,3,4);\nout geom;'))
    assert called


def test_interpreter_route_answers_around_queries_too(local):
    from fastapi.testclient import TestClient

    from backend.main import app

    r = TestClient(app).post("/v1/osm-local/api/interpreter", data={"data": ap.build_airport_query(LAT, LON, 3000)})
    assert r.status_code == 200 and _ids(r.json()["elements"], "way") == {1, 2}


def test_database_built_before_the_extra_selector_stays_fresh_but_declines_relations(tmp_path, monkeypatch):
    old = [s for s in registry()][:-1]          # a database from before relation["aeroway"] existed
    db = tmp_path / "old.sqlite"
    _seed(db, sels=old)
    store = Store(db)
    assert store.fresh
    members = ql.parse_around(ap.build_airport_query(LAT, LON, 3000))
    assert not all(store.supports_around(m) for m in members)   # relation member unsupported -> mirrors
    assert store.supports_around(members[0])                    # ways still known


# ---- traffic basemap: bbox + out geom -----------------------------------------

VIEW = (0.5, 9.5, 0.5, 9.5)  # traffic BBox order: south, north, west, east
BBOX = (VIEW[0], VIEW[2], VIEW[1], VIEW[3])  # parser order: south, west, north, east


def test_the_four_traffic_basemap_queries_parse_and_resolve_their_filters():
    builders = {
        "country": bm.country_query,
        "motorways": bm.motorway_query,
        "waterways": bm.waterway_query,
        "water": bm.water_query,
    }
    for name, build in builders.items():
        members = ql.parse_geom(build(VIEW))
        assert members, name
        assert all(m.bbox == BBOX for m in members)
        known = [(m.kind, m.filter) in BBOX_FILTERS for m in members]
        # The country border has no stored selector: it must stay with Overpass.
        assert all(known) is (name != "country"), name
    reg = set(registry())
    assert all(src in reg for srcs in BBOX_FILTERS.values() for src in srcs)
    # Single member (motorway, river) and union (country, water) are both valid.
    assert len(ql.parse_geom(bm.motorway_query(VIEW))) == 1
    assert len(ql.parse_geom(bm.water_query(VIEW))) == 2


@pytest.mark.parametrize("bad", [
    "[out:json][timeout:25];(way[\"a\"](0,0,1,1););out body;>;out skel qt;",
    "[out:json][timeout:25];(way[\"a\"](0,0,1,1););out geom; drop",
    "[out:json][timeout:25];node(0,0,1,1);out geom;",
    "[out:json][timeout:25];way[\"a\"](around:100,5,5);out geom;",
    "[out:json][timeout:25];way[\"a\"](0,0,1,1);relation[\"b\"](around:10,1,2);out geom;",
    "[out:json][timeout:25];(way[\"a\"](0,0,1,1);out geom;",
    "[out:json][timeout:25];(way[\"a\"](0,0,1,1););way[\"b\"](0,0,1,1);out geom;",
    "[out:json];way[\"a\"](0,0,1,1);out geom;",
    ap.build_airport_query(LAT, LON, 3000),
])
def test_bbox_geom_parser_rejects_everything_else(bad):
    with pytest.raises(ql.QueryError):
        ql.parse_geom(bad)


def test_the_citymap_and_around_parsers_still_decline_the_bbox_shape():
    q = bm.motorway_query(VIEW)
    assert ql.parse_geom(q)
    with pytest.raises(ql.QueryError):
        ql.parse(q)
    with pytest.raises(ql.QueryError):
        ql.parse_around(q)


def test_bbox_filters_recheck_the_exact_tags():
    cases = [
        ('["highway"~"^(motorway|trunk)$"]', {"highway": "trunk"}, True),
        ('["highway"~"^(motorway|trunk)$"]', {"highway": "primary"}, False),
        ('["highway"~"^(motorway|trunk)$"]', {"highway": "motorway_link"}, False),
        ('["waterway"="river"]["name"]', {"waterway": "river", "name": "Duna"}, True),
        ('["waterway"="river"]["name"]', {"waterway": "stream", "name": "X"}, False),
        ('["waterway"="river"]["name"]', {"waterway": "river"}, False),
        ('["natural"="water"]["name"]["water"~"^(lake|reservoir)$"]',
         {"natural": "water", "name": "Lake", "water": "reservoir"}, True),
        ('["natural"="water"]["name"]["water"~"^(lake|reservoir)$"]',
         {"natural": "water", "water": "lake"}, False),
        ('["natural"="water"]["name"]["water"~"^(lake|reservoir)$"]',
         {"natural": "water", "name": "Pond", "water": "pond"}, False),
    ]
    for text, tags, want in cases:
        assert tags_match(text, tags) is want, (text, tags)


def test_run_geom_answers_the_traffic_layers_and_excludes_non_matches(local):
    motorways = local.run_geom(ql.parse_geom(bm.motorway_query(VIEW)))
    assert _ids(motorways["elements"], "way") == {10, 12}
    w10 = next(e for e in motorways["elements"] if e["id"] == 10)
    assert w10["geometry"][0] == {"lat": 7.0, "lon": 1.0}
    assert "nodes" not in w10
    assert motorways["osm3s"]["timestamp_osm_base"] == "2026-11-16T00:00:00Z"

    waterways = local.run_geom(ql.parse_geom(bm.waterway_query(VIEW)))
    assert _ids(waterways["elements"], "way") == {13}  # not the stream or unnamed river

    water = local.run_geom(ql.parse_geom(bm.water_query(VIEW)))
    assert _ids(water["elements"], "way") == {16}      # not unnamed / pond
    assert _ids(water["elements"], "relation") == {901}
    rel = next(e for e in water["elements"] if e["type"] == "relation")
    assert rel["members"][0]["geometry"][0] == {"lat": 7.95, "lon": 1.0}
    assert all(e["type"] != "node" for e in water["elements"])


def test_run_geom_limits_to_the_bbox_and_dedupes_union_members(local):
    small = (6.5, 7.05, 0.5, 3.5)  # lat 7.0 motorway in, lat 8.0 trunk out
    assert _ids(local.run_geom(ql.parse_geom(bm.motorway_query(small)))["elements"], "way") == {10}
    q = (
        '[out:json][timeout:25];\n(\n'
        '  way["highway"~"^(motorway|trunk)$"](0.5,0.5,9.5,9.5);\n'
        '  way["highway"~"^(motorway|trunk)$"](0.5,0.5,9.5,9.5);\n'
        ');\nout geom;'
    )
    ids = [e["id"] for e in local.run_geom(ql.parse_geom(q))["elements"]]
    assert sorted(ids) == [10, 12]


def test_bbox_geom_falls_through_when_uncovered_unsupported_or_stale(local, monkeypatch):
    from backend.osm_local import service

    q = bm.motorway_query(VIEW)
    assert service.answer(q) is not None
    assert service.answer(bm.motorway_query((20.0, 21.0, 20.0, 21.0))) is None
    assert service.answer(bm.country_query(VIEW)) is None
    assert service.answer(
        '[out:json][timeout:25];\nway["highway"="cycleway"](0.5,0.5,9.5,9.5);\nout geom;'
    ) is None
    monkeypatch.setattr("backend.osm_local.store.registry", lambda: [("way", '["nothing"]')])
    service.reset()
    assert service.answer(q) is None


def test_traffic_basemap_fetch_uses_the_extract_only_for_supported_layers(local, monkeypatch):
    got = asyncio.run(ap.fetch_airport_polygons(bm.motorway_query(VIEW)))
    assert _ids(got, "way") == {10, 12}

    async def boom(*a, **k):
        raise AssertionError("mirror must not be called")

    monkeypatch.setattr(ap.OverpassClient, "fetch", boom)
    asyncio.run(ap.fetch_airport_polygons(bm.waterway_query(VIEW)))  # still local

    called = []

    async def mirror(self, query):
        called.append(query)
        return [{"type": "relation", "id": 999}]

    monkeypatch.setattr(ap.OverpassClient, "fetch", mirror)
    out = asyncio.run(ap.fetch_airport_polygons(bm.country_query(VIEW)))
    assert called and out == [{"type": "relation", "id": 999}]


# ---- oversized traffic views are clipped, not declined ------------------------

OVERSIZED = (-50.0, 50.0, -40.0, 40.0)  # traffic BBox: south, north, west, east
OVERSIZED_BBOX = (OVERSIZED[0], OVERSIZED[2], OVERSIZED[1], OVERSIZED[3])


def test_clip_bbox_intersects_the_coverage_but_covers_stays_strict(local):
    assert not local.covers(OVERSIZED_BBOX)
    assert local.clip_bbox(OVERSIZED_BBOX) == (0.0, 0.0, 10.0, 10.0)
    assert local.clip_bbox((20.0, 20.0, 21.0, 21.0)) is None
    assert cov.clip_bbox(local.rings, (10.0, 0.0, 12.0, 10.0)) is None  # edge touch


def test_oversized_traffic_queries_are_answered_from_the_covered_part(local):
    from backend.osm_local import service

    motorways = service.answer(bm.motorway_query(OVERSIZED))
    assert motorways is not None
    assert _ids(motorways["elements"], "way") == {10, 12}
    assert _ids(service.answer(bm.waterway_query(OVERSIZED))["elements"], "way") == {13}
    water = service.answer(bm.water_query(OVERSIZED))
    assert _ids(water["elements"], "way") == {16}
    assert _ids(water["elements"], "relation") == {901}
    # Clipping removed the out-of-coverage way that an unclipped query would see.
    unclipped = local.run_geom(ql.parse_geom(bm.motorway_query(OVERSIZED)))
    assert 20 in _ids(unclipped["elements"], "way")
    assert 20 not in _ids(motorways["elements"], "way")


def test_citymap_and_around_keep_full_coverage_with_the_geom_path(local):
    from backend.osm_local import service

    assert service.answer(build_overpass_query(OVERSIZED_BBOX, ["highways"])) is None
    assert service.answer(ap.build_airport_query(5.0, 5.0, 600_000)) is None
    assert service.answer(bm.motorway_query(OVERSIZED)) is not None


def test_traffic_basemap_fetch_handles_the_real_oversized_view(local, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("mirror must not be called")

    monkeypatch.setattr(ap.OverpassClient, "fetch", boom)
    for query in (bm.motorway_query(OVERSIZED), bm.waterway_query(OVERSIZED),
                  bm.water_query(OVERSIZED)):
        assert asyncio.run(ap.fetch_airport_polygons(query))
