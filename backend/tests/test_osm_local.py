"""Local OSM extract: QL parser, coverage polygon, store, server, importer."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from backend.citymap import overpass as op
from backend.citymap.layers import LAYER_ORDER
from backend.citymap.overpass import build_overpass_query, split_elements
from backend.osm_local import coverage as cov
from backend.osm_local import ql
from backend.osm_local.selectors import registry, selector_mask
from backend.osm_local.store import Store, Writer

# Square "country" 0..10 lon, 0..10 lat with a notch cut out of the east side.
POLY = """hungary
1
   0.0  0.0
   10.0 0.0
   10.0 4.0
   6.0  4.0
   6.0  6.0
   10.0 6.0
   10.0 10.0
   0.0  10.0
END
END
"""


def _seed(path, rings=None):
    sels = registry()
    w = Writer(path)
    # a primary road at lat 1, lon 1..2 ("highways")
    w.add_way(1, selector_mask("way", {"highway": "primary"}, sels), {"highway": "primary"},
              [101, 102], [(1.0, 1.0), (1.0, 2.0)])
    # a building (closed) around lat 2
    w.add_way(2, selector_mask("way", {"building": "yes"}, sels), {"building": "yes"},
              [201, 202, 203, 201], [(2.0, 1.0), (2.0, 1.1), (2.1, 1.1), (2.0, 1.0)])
    # a footway sharing node 102 with the road, far away at lat 9 is separate
    w.add_way(3, selector_mask("way", {"highway": "footway"}, sels), {"highway": "footway"},
              [102, 301], [(1.0, 2.0), (1.5, 2.0)])
    # a far way (lat 9)
    w.add_way(4, selector_mask("way", {"highway": "primary"}, sels), {"highway": "primary"},
              [401, 402], [(9.0, 1.0), (9.0, 2.0)])
    # untagged member way of a water multipolygon (not selectable on its own)
    w.add_way(5, 0, {}, [501, 502, 503, 501], [(3.0, 1.0), (3.0, 1.2), (3.2, 1.2), (3.0, 1.0)],
              keep_bounds=True)
    rel_tags = {"type": "multipolygon", "natural": "water"}
    w.add_relation(900, selector_mask("relation", rel_tags, sels), rel_tags, [(5, "outer")])
    w.finish(rings or cov.parse_poly(POLY), "2026-11-16T00:00:00Z", "test.pbf")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "osm.sqlite"
    _seed(path)
    return path


# ---- parser -----------------------------------------------------------------

def test_parser_accepts_exactly_what_citymap_sends():
    q = build_overpass_query((0.5, 0.5, 5.0, 2.5), ["highways", "water", "ferry"])
    members = ql.parse(q)
    assert {m.kind for m in members} == {"way", "relation"}
    assert all(m.bbox == (0.5, 0.5, 5.0, 2.5) for m in members)
    assert all(m.filter.startswith("[") for m in members)


@pytest.mark.parametrize("bad", [
    "[out:json];node(1,2,3,4);out;",
    "[out:json][timeout:25];(node[\"a\"](0,0,1,1););out body;>;out skel qt;",
    "[out:json][timeout:25];(way[\"a\"](0,0,1,1););out body;>;out skel qt; drop",
    "",
])
def test_parser_rejects_everything_else(bad):
    with pytest.raises(ql.QueryError):
        ql.parse(bad)


def test_every_citymap_selector_is_in_the_registry():
    q = build_overpass_query((0, 0, 1, 1), list(LAYER_ORDER))
    from backend.osm_local.selectors import EXTRA_SELECTORS

    reg = set(registry())
    assert {(m.kind, m.filter) for m in ql.parse(q)} == reg - set(EXTRA_SELECTORS)


# ---- coverage ---------------------------------------------------------------

def test_coverage_polygon_is_exact_at_the_notch():
    rings = cov.parse_poly(POLY)
    assert cov.covers(rings, (1, 1, 3, 3))  # well inside
    assert not cov.covers(rings, (4.5, 5, 5.5, 8))  # bbox reaches into the notch
    assert not cov.covers(rings, (3, 5, 7, 9))  # corners inside, notch edge cuts through
    assert not cov.covers(rings, (-1, 1, 3, 3))  # sticks out
    assert cov.covers(rings, (7, 7, 9, 9))  # north of the notch is inside again


def test_rect_fallback_coverage():
    rings = cov.rect_ring((0, 0, 10, 10))
    assert cov.covers(rings, (1, 1, 2, 2))
    assert not cov.covers(rings, (9, 9, 11, 11))


# ---- store + citymap compatibility -------------------------------------------

def test_store_answers_overpass_shape_and_citymap_can_split_it(db):
    store = Store(db)
    q = build_overpass_query((0.5, 0.5, 4.0, 3.0), ["highways", "buildings", "water", "paths"])
    out = store.run(ql.parse(q))
    kinds = {e["type"] for e in out["elements"]}
    assert kinds == {"node", "way", "relation"}
    ways = {e["id"] for e in out["elements"] if e["type"] == "way"}
    assert ways == {1, 2, 3, 5}  # 4 is far away; 5 arrives through its relation
    geoms, counts = split_elements(out["elements"], ["highways", "buildings", "water", "paths"])
    assert len(geoms["highways"]) == 1 and len(geoms["buildings"]) == 1
    assert len(geoms["paths"]) == 1 and len(geoms["water"]) == 1  # relation member claimed
    lon, lat = geoms["highways"][0][0]
    assert (round(lon, 6), round(lat, 6)) == (1.0, 1.0)
    # shared node 102 is emitted once
    node_ids = [e["id"] for e in out["elements"] if e["type"] == "node"]
    assert len(node_ids) == len(set(node_ids))
    assert out["osm3s"]["timestamp_osm_base"] == "2026-11-16T00:00:00Z"


def test_store_only_returns_what_the_filter_selects(db):
    store = Store(db)
    out = store.run(ql.parse(build_overpass_query((0, 0, 10, 10), ["highways"])))
    assert {e["id"] for e in out["elements"] if e["type"] == "way"} == {1, 4}


# ---- in-process service + citymap wiring ---------------------------------------

@pytest.fixture
def local(db, monkeypatch):
    from backend.osm_local import service

    monkeypatch.setattr(op.settings, "local_osm_db", str(db))
    service.reset()
    yield service
    service.reset()


def test_local_answers_inside_and_declines_outside(local):
    async def _t():
        inside = build_overpass_query((0.5, 0.5, 4.0, 3.0), ["highways"])
        assert (await local.fetch_local(inside))
        notch = build_overpass_query((4.5, 5.0, 5.5, 8.0), ["highways"])
        assert await local.fetch_local(notch) is None  # a public mirror must answer
        assert await local.fetch_local("[out:json];node(1);out;") is None
    asyncio.run(_t())


def test_local_is_off_without_a_database(monkeypatch):
    async def _t():
        from backend.osm_local import service

        monkeypatch.setattr(op.settings, "local_osm_db", "")
        assert await service.fetch_local(build_overpass_query((1, 1, 2, 2), ["highways"])) is None
        monkeypatch.setattr(op.settings, "local_osm_db", "/nonexistent/osm.sqlite")
        service.reset()
        assert await service.fetch_local(build_overpass_query((1, 1, 2, 2), ["highways"])) is None
    asyncio.run(_t())


def test_stale_database_falls_through(local, monkeypatch):
    async def _t():
        monkeypatch.setattr("backend.osm_local.store.registry", lambda: [("way", '["nothing"]')])
        local.reset()
        assert await local.fetch_local(build_overpass_query((1, 1, 2, 2), ["highways"])) is None
    asyncio.run(_t())


def test_fetch_overpass_uses_local_before_the_mirrors(local, monkeypatch):
    async def _t():
        class Boom:
            def __init__(self, *a, **k):
                raise AssertionError("public mirror must not be asked")

        monkeypatch.setattr(op, "OverpassClient", Boom)
        els = await op.fetch_overpass(build_overpass_query((0.5, 0.5, 4.0, 3.0), ["highways"]))
        assert {e["id"] for e in els if e["type"] == "way"} == {1}
    asyncio.run(_t())


def test_fetch_overpass_falls_through_outside_the_extract(local, monkeypatch):
    async def _t():
        seen = {}

        class Fake:
            def __init__(self, cfg, **k):
                seen["urls"] = cfg.urls

            async def fetch(self, query, budget_s=None):
                return [
                {"type": "way", "id": 7, "nodes": [1, 2], "tags": {"highway": "primary"}},
                {"type": "node", "id": 1, "lat": 4.5, "lon": 5.0},
                {"type": "node", "id": 2, "lat": 4.6, "lon": 5.0},
            ]

        monkeypatch.setattr(op, "OverpassClient", Fake)
        els = await op.fetch_overpass(build_overpass_query((4.5, 5.0, 5.5, 8.0), ["highways"]))
        assert seen["urls"] == tuple(op.settings.overpass_urls)
        # public answers are converted to compact ways: inline geometry, no node dicts
        assert els == [{"type": "way", "id": 7, "tags": {"highway": "primary"}, "g": [5.0, 4.5, 5.0, 4.6]}]
    asyncio.run(_t())


def test_local_queries_are_capped_per_worker(local, monkeypatch):
    import threading
    import time

    live = {"now": 0, "max": 0}
    guard = threading.Lock()

    def slow(_q, _min=0.0, _compact=False):
        with guard:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        time.sleep(0.05)
        with guard:
            live["now"] -= 1
        return {"elements": []}

    monkeypatch.setattr(local, "answer", slow)
    monkeypatch.setattr(op.settings, "local_osm_max_concurrent", 1)
    q = build_overpass_query((1, 1, 2, 2), ["highways"])

    async def _t():
        await asyncio.gather(*[local.fetch_local(q) for _ in range(5)])
    asyncio.run(_t())
    assert live["max"] == 1


def test_interpreter_route_has_the_public_overpass_contract(local):
    from backend.osm_local.router import router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    url = "/v1/osm-local/api/interpreter"
    ok = client.post(url, data={"data": build_overpass_query((0.5, 0.5, 4.0, 3.0), ["highways"])})
    assert ok.status_code == 200 and {"version", "elements", "osm3s"} <= set(ok.json())
    out = client.post(url, data={"data": build_overpass_query((4.5, 5.0, 5.5, 8.0), ["highways"])})
    assert out.status_code == 400  # same as a mirror refusing: caller tries the next


def test_health_route_reports_state(local):
    from backend.osm_local.router import router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    body = TestClient(app).get("/v1/osm-local/health").json()
    assert body["status"] == "ok" and body["ways"] == 5


# ---- importer (needs the optional osmium dependency) -------------------------

def test_importer_round_trip(tmp_path):
    osmium = pytest.importorskip("osmium")
    from backend.osm_local.build import build

    pbf = tmp_path / "t.osm.pbf"
    with osmium.SimpleWriter(str(pbf)) as w:
        for nid, (lon, lat) in enumerate(
            [(1.0, 1.0), (1.0, 2.0), (3.0, 1.0), (3.1, 1.0), (3.1, 1.1)], start=1
        ):
            w.add_node(osmium.osm.mutable.Node(id=nid, location=(lon, lat), tags={}))
        w.add_way(osmium.osm.mutable.Way(id=10, nodes=[1, 2], tags={"highway": "primary"}))
        w.add_way(osmium.osm.mutable.Way(id=11, nodes=[3, 4, 5, 3], tags={}))
        w.add_relation(osmium.osm.mutable.Relation(
            id=20, members=[("w", 11, "outer")],
            tags={"type": "multipolygon", "natural": "water"}))
    out = tmp_path / "osm.sqlite"
    stats = build(pbf, out)
    assert stats["ways"] == 2 and stats["relations"] == 1
    store = Store(out)
    res = store.run(ql.parse(build_overpass_query((0.5, 0.5, 3.5, 3.5), ["highways", "water"])))
    assert {e["id"] for e in res["elements"] if e["type"] == "way"} == {10, 11}
    n1 = next(e for e in res["elements"] if e["type"] == "node" and e["id"] == 1)
    assert (n1["lat"], n1["lon"]) == (1.0, 1.0)


def test_uncached_renders_are_capped_per_worker(monkeypatch):
    from backend.citymap import router as cr

    monkeypatch.setattr(cr, "_render_sem", None)
    monkeypatch.setattr(cr.settings, "render_max_concurrent", 2)
    sem = cr._render_slot()
    assert sem._value == 2 and cr._render_slot() is sem


# ---- early length filter -------------------------------------------------------

def test_buckets_floor_and_never_overshoot():
    from backend.citymap.shortways import detail_bucket, threshold_m

    assert [detail_bucket(v) for v in (0, 4.9, 5, 10, 24.9, 25, 99, 100)] == [0, 0, 5, 10, 10, 25, 50, 100]
    assert detail_bucket(30) <= 30 and threshold_m(30) < 30  # never drops what the render keeps
    assert threshold_m(0) == 0


def _way(wid, pts, tags=None):
    return [{"type": "way", "id": wid, "nodes": [wid * 10 + i for i in range(len(pts))], "tags": tags or {}}] + [
        {"type": "node", "id": wid * 10 + i, "lat": la, "lon": lo} for i, (la, lo) in enumerate(pts)
    ]


def test_drop_short_ways_prunes_orphan_nodes_and_keeps_relations():
    from backend.citymap.shortways import drop_short_ways

    short = _way(1, [(47.0, 19.0), (47.00005, 19.0)])  # ~5.5 m
    long_ = _way(2, [(47.0, 19.0), (47.001, 19.0)])  # ~110 m
    unknown = [{"type": "way", "id": 3, "nodes": [999, 998], "tags": {}}]  # no coordinates: keep
    rel = [{"type": "relation", "id": 9, "members": [{"type": "way", "ref": 1, "role": ""}], "tags": {}}]
    out = drop_short_ways(short + long_ + unknown + rel, 25.0)
    ids = {(e["type"], e["id"]) for e in out}
    assert ("way", 1) not in ids and ("way", 2) in ids and ("way", 3) in ids and ("relation", 9) in ids
    assert {n for t, n in ids if t == "node"} == {20, 21}  # only the kept way's nodes
    assert drop_short_ways(short, 0) == short


def test_store_filters_short_ways_in_sql_and_matches_the_post_filter(db):
    from backend.citymap.shortways import drop_short_ways

    store = Store(db)
    assert store.has_len
    q = ql.parse(build_overpass_query((0.5, 0.5, 4.0, 3.0), ["highways", "buildings", "paths", "water"]))
    full = store.run(q)["elements"]
    ways_full = {e["id"] for e in full if e["type"] == "way"}
    sql = store.run(q, 150_000.0)["elements"]  # 150 km: everything in the fixture is shorter
    assert not [e for e in sql if e["type"] == "way"]
    mid = store.run(q, 100_000.0)["elements"]  # keeps only the 111 km road
    assert {e["id"] for e in mid if e["type"] == "way"} == {1}
    assert {e["id"] for e in drop_short_ways(full, 100_000.0) if e["type"] == "way"} == {1}
    assert ways_full >= {1, 2, 3, 5}


def test_schema_v1_database_still_works_with_a_post_filter(tmp_path):
    import sqlite3

    path = tmp_path / "v1.sqlite"
    _seed(path)
    con = sqlite3.connect(path)
    con.execute("UPDATE meta SET value='1' WHERE key='schema'")
    con.commit()
    con.close()
    store = Store(path)
    assert not store.has_len
    q = ql.parse(build_overpass_query((0.5, 0.5, 4.0, 3.0), ["highways", "buildings"]))
    out = store.run(q, 100_000.0)["elements"]
    assert {e["id"] for e in out if e["type"] == "way"} == {1}


def test_tile_and_split_keys_carry_the_bucket():
    from backend.citymap.render_cache import _split_key, _tile_key

    bb = (47.0, 19.0, 47.1, 19.1)
    assert _tile_key((1, 2), 0.02, "roads", "m10") != _tile_key((1, 2), 0.02, "roads", "m25")
    assert _split_key(bb, ["roads"], "m10") != _split_key(bb, ["roads"], "m0")


# ---- compact elements -----------------------------------------------------------

def test_compact_pipeline_is_equivalent_to_the_node_pipeline(db):
    """Node-form and compact elements must tile, clip and split identically."""
    from backend.citymap.shortways import compact_elements
    from backend.citymap.tiles import assign_to_tiles, clip_elements

    store = Store(db)
    layers = ["highways", "buildings", "paths", "water"]
    q = ql.parse(build_overpass_query((0.5, 0.5, 4.0, 3.0), layers))
    node_form = store.run(q)["elements"]
    compact = store.run(q, compact=True)["elements"]
    assert not [e for e in compact if e["type"] == "node"]  # node dicts never built
    assert compact_elements(node_form) and {
        (e["type"], e["id"]) for e in compact_elements(node_form)
    } == {(e["type"], e["id"]) for e in compact}
    box = (0.9, 0.9, 2.5, 2.2)
    for els in (node_form, compact):
        by_tile = assign_to_tiles(els, 1.0)
        merged = [e for tile in sorted(by_tile) for e in by_tile[tile]]
        clipped = clip_elements(merged, box)
        geoms, _ = split_elements(clipped, layers)
        key = {k: sorted(tuple(map(tuple, p)) for p in v) for k, v in geoms.items()}
        if els is node_form:
            want = key
    assert key == want and any(want.values())


def test_compact_elements_is_idempotent_and_filters_by_length():
    from backend.citymap.shortways import compact_elements

    els = _way(1, [(47.0, 19.0), (47.00005, 19.0)]) + _way(2, [(47.0, 19.0), (47.001, 19.0)])
    once = compact_elements(els, 25.0)
    assert [w["id"] for w in once] == [2] and once[0]["g"] == [19.0, 47.0, 19.0, 47.001]
    assert compact_elements(once, 25.0) == once
    assert [w["id"] for w in compact_elements(els)] == [1, 2]
