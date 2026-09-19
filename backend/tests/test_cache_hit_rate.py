"""Router-level cache behaviour: the things that actually move hit rate.

Each test here pins one production symptom observed in the logs:
a viewport nudge missing, a layer toggle refetching everything, an SVG hit
still doing the work of a miss, and a slider tick costing an upstream call.
"""

from __future__ import annotations

import asyncio

import backend.airports.router as airports_router
import backend.citymap.router as citymap_router
from backend.airports.cache import configure_airports_redis
from backend.citymap.cache import configure_citymap_redis
from backend.citymap.schemas import RenderRequest


def _elements():
    """A few highways around Budapest, spread over several tiles."""
    out = []
    node_id = 1
    way_id = 1
    lat = 47.460
    while lat < 47.540:
        lon = 19.040
        while lon < 19.140:
            out.append({"type": "node", "id": node_id,
                        "lat": lat, "lon": lon})
            out.append({"type": "node", "id": node_id + 1,
                        "lat": lat + 0.001, "lon": lon + 0.001})
            out.append({"type": "way", "id": way_id,
                        "nodes": [node_id, node_id + 1],
                        "tags": {"highway": "motorway"}})
            node_id += 2
            way_id += 1
            lon += 0.010
        lat += 0.010
    return out


class _CountingUpstream:
    """Stands in for Overpass and records every query it is asked for."""

    def __init__(self, elements):
        self.elements = elements
        self.queries: list[str] = []

    async def __call__(self, query, client=None):
        self.queries.append(query)
        return list(self.elements)


def _patch(monkeypatch_target, upstream):
    orig = citymap_router.fetch_overpass
    citymap_router.fetch_overpass = upstream
    return orig


def _fresh_caches():
    configure_citymap_redis(None)
    configure_airports_redis(None)


def _load(bbox, layers, warnings=None):
    return asyncio.run(
        citymap_router._load_raw(bbox, layers, warnings if warnings is not None else [])
    )


def test_identical_bbox_hits_without_touching_upstream():
    _fresh_caches()
    upstream = _CountingUpstream(_elements())
    orig = _patch(citymap_router, upstream)
    try:
        bbox = (47.46, 19.04, 47.54, 19.14)
        first, err = _load(bbox, ["highways"])
        assert err is None and first
        assert len(upstream.queries) == 1

        warnings: list[str] = []
        second, err = _load(bbox, ["highways"], warnings)
        assert err is None
        assert len(upstream.queries) == 1, "a repeat must not refetch"
        assert "overpass_cache_hit" in warnings
        assert {(e["type"], e["id"]) for e in second} == {
            (e["type"], e["id"]) for e in first
        }
    finally:
        citymap_router.fetch_overpass = orig


def _queried_area(query: str) -> float:
    """Total square degrees the bbox clauses in an Overpass query cover."""
    import re

    total = 0.0
    for s, w, n, e in re.findall(
        r"\((-?[\d.]+),(-?[\d.]+),(-?[\d.]+),(-?[\d.]+)\)", query
    ):
        total += (float(n) - float(s)) * (float(e) - float(w))
    return total


def test_panning_refetches_only_the_new_tiles():
    """The production symptom: every pan was a full refetch.

    After a pan the second query must cover only the newly exposed strip,
    because the overlapping tiles are served from cache.
    """
    _fresh_caches()
    upstream = _CountingUpstream(_elements())
    orig = _patch(citymap_router, upstream)
    try:
        _load((47.46, 19.04, 47.54, 19.12), ["highways"])
        assert len(upstream.queries) == 1
        _load((47.46, 19.06, 47.54, 19.14), ["highways"])  # panned east
        assert len(upstream.queries) == 2, "the new column must be fetched"

        first, second = (_queried_area(q) for q in upstream.queries)
        # The pan exposed a quarter of the width, so the follow-up fetch
        # should be about a quarter the area — and certainly not a repeat.
        assert second < first / 3, (
            f"pan refetched {second:.4f} deg^2 of the original {first:.4f}"
        )
    finally:
        citymap_router.fetch_overpass = orig


def test_adding_a_layer_keeps_the_layers_already_held():
    """`layers=highways,roads` -> `+paths` used to refetch all three."""
    _fresh_caches()
    upstream = _CountingUpstream(_elements())
    orig = _patch(citymap_router, upstream)
    try:
        bbox = (47.46, 19.04, 47.54, 19.14)
        _load(bbox, ["highways"])
        assert len(upstream.queries) == 1
        _load(bbox, ["highways", "roads"])
        assert len(upstream.queries) == 2
        # Only the new layer may appear in the follow-up query.
        assert "highway" in upstream.queries[1]
        assert "motorway" not in upstream.queries[1], (
            "highways was cached and must not be refetched"
        )
    finally:
        citymap_router.fetch_overpass = orig


def test_a_tile_with_no_features_is_remembered_as_empty():
    """An empty answer is an answer; it must not be re-fetched forever."""
    _fresh_caches()
    upstream = _CountingUpstream([])
    orig = _patch(citymap_router, upstream)
    try:
        bbox = (47.46, 19.04, 47.48, 19.06)
        assert _load(bbox, ["highways"]) == ([], None)
        assert len(upstream.queries) == 1
        assert _load(bbox, ["highways"]) == ([], None)
        assert len(upstream.queries) == 1, "empty tiles must be cached too"
    finally:
        citymap_router.fetch_overpass = orig


def _body(bbox=(47.46, 19.04, 47.54, 19.14), **kw):
    south, west, north, east = bbox
    return RenderRequest(
        bbox={"south": south, "west": west, "north": north, "east": east},
        layers=["highways"], **kw,
    )


def test_nearby_viewports_collapse_onto_one_svg_key():
    """Raw viewport floats are unique per pan; snapping collapses them.

    Two of the bboxes seen in production differ by ~1 m and produced two
    entirely separate renders. After snapping they share a key.
    """
    from backend.citymap.config import settings
    from backend.citymap.tiles import snap_bbox

    deg = settings.render_snap_deg
    raw_a = (47.45556, 19.02831, 47.54440, 19.20169)
    raw_b = (47.45560, 19.02835, 47.54438, 19.20166)
    assert raw_a != raw_b

    body = _body()
    key_a = citymap_router._render_keys(snap_bbox(raw_a, deg), ["highways"], body)
    key_b = citymap_router._render_keys(snap_bbox(raw_b, deg), ["highways"], body)
    assert key_a == key_b

    # A genuinely different area must still get its own key.
    far = snap_bbox((47.30, 19.02, 47.39, 19.20), deg)
    assert citymap_router._render_keys(far, ["highways"], body) != key_a


def test_render_keys_separate_svg_from_its_counts_sidecar():
    svg_key, counts_key = citymap_router._render_keys(
        (47.46, 19.04, 47.54, 19.14), ["highways"], _body()
    )
    assert svg_key != counts_key
    assert ":svg:" in svg_key and ":counts:" in counts_key


def test_render_params_still_separate_svg_keys():
    """Snapping must not blur together genuinely different renders."""
    bbox = (47.46, 19.04, 47.54, 19.14)
    base, _ = citymap_router._render_keys(bbox, ["highways"], _body())
    wider, _ = citymap_router._render_keys(bbox, ["highways"], _body(width=2000))
    longer, _ = citymap_router._render_keys(
        bbox, ["highways"], _body(min_path_len_m=50.0))
    layered, _ = citymap_router._render_keys(
        bbox, ["highways", "roads"], _body())
    assert len({base, wider, longer, layered}) == 4


def test_cached_render_round_trips_counts_without_recomputing():
    """An SVG hit must return stored counts, not re-derive them."""
    _fresh_caches()
    svg_key, counts_key = "k:svg:1", "k:counts:1"
    path_counts = {"highways": 42}
    raw_counts = {"nodes": 7, "ways": 3, "relations": 1}
    asyncio.run(citymap_router._store_render(
        svg_key, counts_key, "<svg/>", path_counts, raw_counts))

    warnings: list[str] = []
    got = asyncio.run(citymap_router._load_cached_render(
        svg_key, counts_key, ["highways"], warnings))
    assert got == ("<svg/>", path_counts, raw_counts)
    assert "svg_cache_hit" in warnings
    assert "counts_from_cached_svg" not in warnings


def test_missing_sidecar_falls_back_to_counting_the_document():
    """An SVG cached before the sidecar existed must still serve."""
    _fresh_caches()
    from backend.citymap.cache import cache_set

    svg = '<svg><g id="citymap-highways"><path d="M0 0"/><path d="M1 1"/></g></svg>'
    asyncio.run(cache_set("k:svg:2", svg))
    warnings: list[str] = []
    got = asyncio.run(citymap_router._load_cached_render(
        "k:svg:2", "k:counts:2", ["highways"], warnings))
    assert got is not None
    assert got[1] == {"highways": 2}
    assert "counts_from_cached_svg" in warnings


def test_airport_radius_buckets_absorb_slider_ticks():
    """Neighbouring slider positions must not each cost an Overpass call."""
    from backend.airports.config import settings

    bucket = settings.radius_bucket_m
    assert airports_router._bucket_radius_m(3001) == \
        airports_router._bucket_radius_m(3499) == 3500
    # Rounding is always upward, so the fetch never shrinks below the
    # radius the renderer was promised.
    for r in (2500.0, 2501.0, 2999.9, 3000.0):
        assert airports_router._bucket_radius_m(r) >= r
        assert airports_router._bucket_radius_m(r) % bucket == 0


def test_airport_render_keys_include_the_renderer_version():
    """A renderer change must still bust cached SVGs."""
    from backend.airports.schemas import RenderRequest as ARequest

    body = ARequest(icao="LHBP")
    svg_key, meta_key = airports_router._render_keys("LHBP", 3500.0, body)
    assert svg_key != meta_key
    assert ":svg:" in svg_key and ":meta:" in meta_key

    other, _ = airports_router._render_keys("LHBP", 4000.0, body)
    assert other != svg_key, "radius must still separate diagrams"


def test_airport_cached_render_requires_both_halves():
    """Without the sidecar the response cannot be built, so it is a miss."""
    _fresh_caches()
    from backend.airports.cache import cache_set

    asyncio.run(cache_set("a:svg:1", "<svg/>"))
    warnings: list[str] = []
    assert asyncio.run(airports_router._load_cached_render(
        "a:svg:1", "a:meta:1", warnings)) is None
    assert warnings == []

    asyncio.run(airports_router._store_render(
        "a:svg:1", "a:meta:1", "<svg/>", {"runway": 9}, {"ways": 4}, 13.5))
    got = asyncio.run(airports_router._load_cached_render(
        "a:svg:1", "a:meta:1", warnings))
    assert got == ("<svg/>", {"runway": 9}, {"ways": 4}, 13.5)
    assert "svg_cache_hit" in warnings
