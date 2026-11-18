"""Airports and airport traffic use the same local extract as citymap (``around`` + ``out geom``)."""

from __future__ import annotations

import asyncio

import pytest

from backend.airports import overpass as ap
from backend.airports.sources import build_reduced_airport_query
from backend.osm_local import coverage as cov
from backend.osm_local import ql
from backend.osm_local.selectors import AROUND_FILTERS, registry, selector_mask
from backend.osm_local.store import Store, Writer

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
    asyncio.run(ap.fetch_airport_polygons('[out:json][timeout:90];\nway["highway"~"^(motorway|trunk)$"](1,2,3,4);\nout geom;'))
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
