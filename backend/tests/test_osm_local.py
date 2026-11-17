"""Local OSM extract: QL parser, coverage polygon, store, server, importer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.citymap import overpass as op
from backend.citymap.layers import LAYER_ORDER
from backend.citymap.overpass import build_overpass_query, split_elements
from backend.osm_local import coverage as cov
from backend.osm_local import ql
from backend.osm_local.selectors import registry, selector_mask
from backend.osm_local.server import create_app
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
    reg = set(registry())
    assert {(m.kind, m.filter) for m in ql.parse(q)} == reg


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


# ---- server ------------------------------------------------------------------

def test_server_serves_inside_and_declines_outside(db):
    client = TestClient(create_app(db))
    ok = client.post("/api/interpreter",
                     data={"data": build_overpass_query((0.5, 0.5, 4.0, 3.0), ["highways"])})
    assert ok.status_code == 200 and ok.json()["elements"]
    outside = client.post("/api/interpreter",
                          data={"data": build_overpass_query((4.5, 5.0, 5.5, 8.0), ["highways"])})
    assert outside.status_code == 400  # in the notch: let a public mirror answer
    junk = client.post("/api/interpreter", data={"data": "[out:json];node(1);out;"})
    assert junk.status_code == 400
    assert client.get("/health").json()["status"] == "ok"


def test_server_refuses_a_stale_database(db, monkeypatch):
    monkeypatch.setattr("backend.osm_local.store.registry", lambda: [("way", '["nothing"]')])
    client = TestClient(create_app(db))
    assert client.get("/health").json()["status"] == "stale"
    r = client.post("/api/interpreter",
                    data={"data": build_overpass_query((1, 1, 2, 2), ["highways"])})
    assert r.status_code == 400


# ---- citymap wiring ----------------------------------------------------------

def test_local_mirror_is_walked_first_and_only_when_configured(monkeypatch):
    public = tuple(op.settings.overpass_urls)
    monkeypatch.setattr(op.settings, "local_osm_url", "")
    assert op.mirror_urls() == public
    monkeypatch.setattr(op.settings, "local_osm_url", "http://localhost:8200/api/interpreter")
    assert op.mirror_urls() == ("http://localhost:8200/api/interpreter", *public)


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
