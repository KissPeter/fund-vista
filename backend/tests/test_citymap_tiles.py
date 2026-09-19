"""Tile-cache invariants for the citymap Overpass cache.

The whole scheme rests on one property: assembling a bbox out of cached
tiles must yield exactly the element set a single query over that bbox
would have returned. ``render_svg`` scales to the extent of the data rather
than to the bbox, so one extra way on an edge silently rescales the entire
drawing — a correctness bug that would look like a rendering glitch.
"""

from __future__ import annotations

import random

from backend.citymap.tiles import (
    assign_to_tiles,
    clip_elements,
    covering_rects,
    rect_bbox,
    snap_bbox,
    tile_deg_for_bbox,
    tile_of_point,
    tile_ref,
    tiles_for_bbox,
)
from backend.citymap.osm_api import merge_elements


def _key(elements):
    """Order-insensitive identity of an element set."""
    return sorted((e["type"], e["id"]) for e in elements)


def _synthetic_city(seed: int, n_ways: int = 300):
    """Ways of 2-6 nodes scattered over a ~0.2 x 0.2 deg patch."""
    rnd = random.Random(seed)
    elements = []
    node_id = 1
    for wid in range(1, n_ways + 1):
        lat = rnd.uniform(47.40, 47.60)
        lon = rnd.uniform(19.00, 19.20)
        refs = []
        for _ in range(rnd.randint(2, 6)):
            # Segments wander far enough to straddle tile boundaries.
            lat += rnd.uniform(-0.012, 0.012)
            lon += rnd.uniform(-0.012, 0.012)
            elements.append(
                {"type": "node", "id": node_id, "lat": lat, "lon": lon}
            )
            refs.append(node_id)
            node_id += 1
        elements.append(
            {"type": "way", "id": wid, "nodes": refs,
             "tags": {"highway": "residential"}}
        )
    return elements


def test_tile_assembly_matches_a_single_query():
    """Merged tiles, clipped to the bbox, equal the direct answer."""
    elements = _synthetic_city(seed=7)
    for bbox in [
        (47.45, 19.02, 47.55, 19.12),
        (47.42, 19.05, 47.47, 19.09),
        (47.50, 19.00, 47.60, 19.20),
    ]:
        direct = clip_elements(elements, bbox)
        deg = tile_deg_for_bbox(bbox)
        by_tile = assign_to_tiles(elements, deg)
        wanted = tiles_for_bbox(bbox, deg)
        assembled = clip_elements(
            merge_elements([by_tile.get(t, []) for t in wanted]), bbox
        )
        assert _key(assembled) == _key(direct), f"mismatch for {bbox} at z{deg}"


def test_assembly_is_independent_of_tile_level():
    """A coarser or finer grid must not change what gets rendered."""
    elements = _synthetic_city(seed=11)
    bbox = (47.46, 19.04, 47.54, 19.14)
    direct = _key(clip_elements(elements, bbox))
    for deg in (0.005, 0.01, 0.02, 0.04, 0.08):
        by_tile = assign_to_tiles(elements, deg)
        assembled = clip_elements(
            merge_elements(
                [by_tile.get(t, []) for t in tiles_for_bbox(bbox, deg)]
            ),
            bbox,
        )
        assert _key(assembled) == direct, f"z{deg} changed the element set"


def test_partial_cache_reuse_still_exact():
    """A pan that reuses old tiles and fetches new ones is still exact.

    Models the real sequence: render bbox A, then bbox B overlapping it.
    B's shared tiles come from A's fetch, the rest from a fetch limited to
    B's missing rectangles — the assembled result must still be exact.
    """
    elements = _synthetic_city(seed=23)
    bbox_a = (47.46, 19.02, 47.54, 19.10)
    bbox_b = (47.46, 19.06, 47.54, 19.14)  # panned east
    deg = tile_deg_for_bbox(bbox_a)
    assert tile_deg_for_bbox(bbox_b) == deg

    tiles_a = set(tiles_for_bbox(bbox_a, deg))
    tiles_b = set(tiles_for_bbox(bbox_b, deg))
    assert tiles_a & tiles_b, "test needs overlapping tiles to be meaningful"

    # First render populates A's tiles from a fetch over A.
    cache = dict(assign_to_tiles(clip_elements(elements, bbox_a), deg, limit_to=tiles_a))

    # Second render fetches only what it lacks, over those tiles' rectangles.
    missing = tiles_b - set(cache)
    covered = set()
    fetched = []
    for rect in covering_rects(missing):
        rb = rect_bbox(rect, deg)
        row0, col0, row1, col1 = rect
        covered |= {
            (r, c) for r in range(row0, row1 + 1) for c in range(col0, col1 + 1)
        }
        fetched.append(clip_elements(elements, rb))
    cache.update(assign_to_tiles(merge_elements(fetched), deg, limit_to=covered))

    assembled = clip_elements(
        merge_elements([cache.get(t, []) for t in tiles_for_bbox(bbox_b, deg)]),
        bbox_b,
    )
    assert _key(assembled) == _key(clip_elements(elements, bbox_b))


def test_relations_travel_with_their_members():
    """A relation's member ways ride along into every tile it reaches."""
    elements = [
        {"type": "node", "id": 1, "lat": 47.401, "lon": 19.001},
        {"type": "node", "id": 2, "lat": 47.409, "lon": 19.009},
        {"type": "node", "id": 3, "lat": 47.431, "lon": 19.031},
        {"type": "node", "id": 4, "lat": 47.439, "lon": 19.039},
        {"type": "way", "id": 10, "nodes": [1, 2], "tags": {}},
        {"type": "way", "id": 11, "nodes": [3, 4], "tags": {}},
        {"type": "relation", "id": 99, "tags": {"route": "tram"},
         "members": [
             {"type": "way", "ref": 10, "role": ""},
             {"type": "way", "ref": 11, "role": ""},
         ]},
    ]
    by_tile = assign_to_tiles(elements, 0.02)
    holding = [t for t, payload in by_tile.items()
               if any(e["type"] == "relation" for e in payload)]
    assert len(holding) == 2, "relation should reach both members' tiles"
    for tile in holding:
        ids = {(e["type"], e["id"]) for e in by_tile[tile]}
        assert ("way", 10) in ids and ("way", 11) in ids
        # `>` would have pulled every member node in with the relation.
        assert {("node", n) for n in (1, 2, 3, 4)} <= ids


def test_empty_bbox_yields_nothing():
    elements = _synthetic_city(seed=5)
    assert clip_elements(elements, (10.0, 10.0, 10.1, 10.1)) == []


def test_snap_bbox_expands_outward_and_is_idempotent():
    bbox = (47.45568, 18.94540, 47.54428, 19.28460)
    snapped = snap_bbox(bbox, 0.002)
    assert snapped[0] <= bbox[0] and snapped[1] <= bbox[1]
    assert snapped[2] >= bbox[2] and snapped[3] >= bbox[3]
    assert snap_bbox(snapped, 0.002) == snapped
    # Nearby viewports collapse onto the same key, which is the point.
    nudged = (47.45570, 18.94541, 47.54427, 19.28459)
    assert snap_bbox(nudged, 0.002) == snapped


def test_tile_level_bounds_the_key_count():
    """Every allowed bbox stays under the tile budget."""
    for bbox in [
        (47.4550, 19.0280, 47.5444, 19.2017),
        (47.4557, 18.9454, 47.5443, 19.2846),
        (47.0, 18.6, 47.8, 19.4),  # the widest bbox max_bbox_deg permits
        (47.4600, 19.0400, 47.4610, 19.0410),  # a single block
    ]:
        deg = tile_deg_for_bbox(bbox, max_tiles=64)
        assert len(tiles_for_bbox(bbox, deg)) <= 64, bbox


def test_tile_level_depends_on_size_not_position():
    """Equal-sized bboxes must land on the same grid wherever they sit.

    Grid sizes like 0.01 are not binary-representable, so 19.06 / 0.01 is
    1905.9999999999998. A bare floor counted an extra column, pushed the
    bbox over the tile budget and picked a coarser level than its neighbour
    one pan away — two adjacent views would then share no tiles at all.
    """
    span = 0.08
    levels = set()
    for west in [19.00 + 0.01 * i for i in range(12)]:
        bbox = (47.46, west, 47.46 + span, west + span)
        levels.add(tile_deg_for_bbox(bbox))
    assert len(levels) == 1, f"level varies with position: {sorted(levels)}"


def test_tile_counts_are_exact_on_grid_boundaries():
    """A bbox already on the grid spans exactly its own tiles, no fringe."""
    assert len(tiles_for_bbox((47.46, 19.06, 47.54, 19.14), 0.01)) == 64
    assert len(tiles_for_bbox((47.46, 19.06, 47.48, 19.08), 0.02)) == 1


def test_tile_ref_separates_levels():
    """The same row/col at different levels must never share a key."""
    assert tile_ref((100, 200), 0.01) != tile_ref((100, 200), 0.02)


def test_tile_of_point_agrees_with_tiles_for_bbox():
    deg = 0.02
    lat, lon = 47.4712, 19.0631
    assert tile_of_point(lat, lon, deg) in tiles_for_bbox(
        (lat, lon, lat, lon), deg
    )


def test_covering_rects_merges_a_strip_into_one_rectangle():
    tiles = {(r, 5) for r in range(10)}
    assert covering_rects(tiles) == [(0, 5, 9, 5)]


def test_covering_rects_covers_every_tile():
    rnd = random.Random(3)
    tiles = {(rnd.randint(0, 6), rnd.randint(0, 6)) for _ in range(25)}
    covered = set()
    for row0, col0, row1, col1 in covering_rects(tiles):
        covered |= {
            (r, c) for r in range(row0, row1 + 1) for c in range(col0, col1 + 1)
        }
    assert tiles <= covered
