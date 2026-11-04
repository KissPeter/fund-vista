"""Layer-by-layer fetch and partial-data discipline for /v1/citymap.

A blank area caused by a failed upstream cell or layer must never be cached
as a real (empty) answer, and a failing heavy layer must not cost the
layers already fetched.
"""

from __future__ import annotations

import asyncio

import backend.citymap.fetching as fetching_mod
from backend.citymap.osm_api import OsmApiError, PartialElements
from backend.citymap.overpass import OverpassError
from backend.citymap.render_cache import _tile_key
from backend.citymap.tiles import tile_deg_for_bbox, tiles_for_bbox

# Unique per test so the (possibly shared) tile cache never carries state.
_BBOX_A = (47.2011, 19.2011, 47.2511, 19.2511)
_BBOX_B = (47.3011, 19.3011, 47.3511, 19.3511)

_HIGHWAY_ELEMENTS = [
    {"type": "node", "id": 1, "lat": 47.22, "lon": 19.22},
    {"type": "node", "id": 2, "lat": 47.23, "lon": 19.23},
    {"type": "way", "id": 10, "nodes": [1, 2], "tags": {"highway": "motorway"}},
]


def _patch(monkeypatch, overpass, osm=None):
    monkeypatch.setattr(fetching_mod, "fetch_overpass", overpass)
    if osm is not None:
        monkeypatch.setattr(fetching_mod, "fetch_osm_api", osm)


def _capture_cache(monkeypatch) -> dict[str, str]:
    """Empty read cache; collect every tile write."""
    written: dict[str, str] = {}

    async def get_many(_keys):
        return {}

    async def set_many(items, _ttl=None):
        written.update(items)
        return len(items)

    monkeypatch.setattr(fetching_mod, "cache_get_many", get_many)
    monkeypatch.setattr(fetching_mod, "cache_set_many", set_many)
    return written


def _tile_keys(bbox, layer) -> list[str]:
    deg = tile_deg_for_bbox(bbox, fetching_mod.settings.tile_max_tiles)
    return [_tile_key(t, deg, layer) for t in tiles_for_bbox(bbox, deg)]


def test_failed_heavy_layer_keeps_other_layers(monkeypatch):
    queries: list[str] = []

    async def overpass(query, _client=None):
        queries.append(query)
        if "building" in query:
            raise OverpassError("buildings timed out")
        return list(_HIGHWAY_ELEMENTS)

    async def dead_osm(_bbox, _client=None):
        raise OsmApiError("osm api 509")

    _patch(monkeypatch, overpass, dead_osm)
    written = _capture_cache(monkeypatch)
    warnings: list[str] = []
    elements, err = asyncio.run(
        fetching_mod._load_raw(_BBOX_A, ["buildings", "highways"], warnings)
    )
    assert err is None
    assert {e["id"] for e in elements if e["type"] == "way"} == {10}
    assert "partial_data" in warnings
    assert "layers_pending:buildings" in warnings
    assert "layers_pending:highways" not in warnings
    # Buildings are fetched last, whatever order the caller listed them in.
    assert "building" not in queries[0] and "building" in queries[-1]
    # Highways were stored per layer; the failed layer stored nothing.
    assert set(_tile_keys(_BBOX_A, "highways")) <= set(written)
    assert not set(_tile_keys(_BBOX_A, "buildings")) & set(written)


def test_osm_api_lost_cells_are_not_cached_as_empty(monkeypatch):
    south, west, north, east = _BBOX_B

    async def boom(_query, _client=None):
        raise OverpassError("all mirrors 504")

    async def partial(_bbox, _client=None):
        # The whole western half of the area was lost (e.g. a 429 parked
        # the endpoint after the first cell).
        mid = (west + east) / 2
        return PartialElements([], [(south, west, north, mid)])

    _patch(monkeypatch, boom, partial)
    written = _capture_cache(monkeypatch)
    warnings: list[str] = []
    elements, err = asyncio.run(
        fetching_mod._load_raw(_BBOX_B, ["highways"], warnings)
    )
    assert err is None
    assert "osm_api_fallback" in warnings
    assert "partial_data" in warnings
    assert "layers_pending:highways" in warnings
    keys = _tile_keys(_BBOX_B, "highways")
    stored = [k for k in keys if k in written]
    # The lost half is not pinned as an empty answer; the rest is kept.
    assert 0 < len(stored) < len(keys)


def test_complete_fetch_is_not_flagged_partial(monkeypatch):
    async def overpass(_query, _client=None):
        return list(_HIGHWAY_ELEMENTS)

    _patch(monkeypatch, overpass)
    _capture_cache(monkeypatch)
    warnings: list[str] = []
    bbox = (47.4011, 19.4011, 47.4511, 19.4511)
    _, err = asyncio.run(fetching_mod._load_raw(bbox, ["highways"], warnings))
    assert err is None
    assert not any(w.startswith(("partial_data", "layers_pending")) for w in warnings)
