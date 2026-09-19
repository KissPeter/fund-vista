"""Grid tiling for the citymap Overpass cache.

Why tiles
---------
The cache used to key raw OSM payloads on ``(exact bbox, whole layer set)``.
Both halves of that key are hostile to reuse: the UI sends raw
``map.getBounds()`` floats, so every pan, zoom or window resize produced a
brand-new 5-decimal (~1 m) key, and adding one layer refetched every layer.
In production that meant near-zero reuse and repeated 25-40 MB fetches of
the same city.

Here the unit of caching is ``(tile, single layer)``. A render covers its
bbox with grid tiles, reads the ones it has, fetches only what is missing,
and merges. Panning east reuses every tile but the new column; ticking a
layer on reuses the layers already held.

Exactness
---------
``render_svg`` scales to the extent of the *data*, not to the bbox, so the
tile set must reproduce exactly what a single Overpass query would have
returned — an extra way on the edge would rescale the whole drawing.

Overpass selects a way when at least one of its nodes lies in the bbox, and
``>; out skel qt;`` then pulls in that way's remaining nodes (and, for a
relation, its member ways). That rule partitions cleanly: the ways with a
node in a region are exactly the union, over tiles covering it, of the ways
with a node in that tile. :func:`assign_to_tiles` applies the rule tile by
tile and :func:`clip_elements` re-applies it to the final bbox, so the
element set handed to the renderer never depends on how it was tiled or on
which tiles happened to be cached.

One thing does change: ``<path>`` elements come out grouped by tile rather
than in Overpass's quadtree order. The geometry, the path count and the
rendered extent are identical — verified against live Overpass — and the
penplot convert step reorders strokes for pen travel anyway.

Zoom levels
-----------
One fixed tile size cannot serve both a neighbourhood and a whole metro —
0.02 deg tiles would need 1600 keys for the largest allowed bbox. Tiles
therefore come in fixed levels (:data:`TILE_LEVELS`) and a bbox
deterministically picks the finest level that covers it in at most
``max_tiles`` tiles. The level is part of the key, so levels never mix;
requests at a similar zoom (the common case — a user panning around one
city) land on the same level and share tiles.
"""

from __future__ import annotations

import math

BBox = tuple[float, float, float, float]  # south, west, north, east

# Fixed tile sizes in degrees, finest first. Each is a whole multiple of the
# next finer one so a coarse tile's bounds always fall on the fine grid too,
# which keeps snapped bboxes stable when a bbox crosses a level boundary.
TILE_LEVELS: tuple[float, ...] = (0.005, 0.01, 0.02, 0.04, 0.08, 0.16)

# Coordinates are rounded before they become keys or bounds: 1e-7 deg is
# ~1 cm, far below OSM precision, and keeps float drift out of the key.
_QUANT = 7


def _round(value: float) -> float:
    return round(value, _QUANT)


def _grid(value: float, deg: float) -> float:
    """``value / deg``, with binary-float noise rounded off.

    Grid sizes like 0.01 are not representable, so ``19.06 / 0.01`` is
    1905.9999999999998 and a bare floor lands a whole tile too low. Left
    alone that made two identically-sized bboxes pick different tile levels
    depending only on where they sat, so panning would thrash between grids
    and share nothing — the exact failure this module exists to prevent.
    """
    return round(value / deg, 9)


def _floor(value: float, deg: float) -> int:
    return math.floor(_grid(value, deg))


def _ceil(value: float, deg: float) -> int:
    return math.ceil(_grid(value, deg))


def snap_bbox(bbox: BBox, deg: float) -> BBox:
    """Expand ``bbox`` outward to the nearest ``deg`` grid lines.

    Used both to pick tile bounds and to stabilise the rendered bbox: the
    viewport floats the UI sends are unique per pan, so snapping them is
    what lets the SVG cache hit at all across small viewport moves.
    """
    south, west, north, east = bbox
    return (
        _round(_floor(south, deg) * deg),
        _round(_floor(west, deg) * deg),
        _round(_ceil(north, deg) * deg),
        _round(_ceil(east, deg) * deg),
    )


def tile_deg_for_bbox(bbox: BBox, max_tiles: int = 64) -> float:
    """Finest level covering ``bbox`` in at most ``max_tiles`` tiles."""
    south, west, north, east = bbox
    for deg in TILE_LEVELS:
        rows = _ceil(north, deg) - _floor(south, deg)
        cols = _ceil(east, deg) - _floor(west, deg)
        if max(rows, 1) * max(cols, 1) <= max_tiles:
            return deg
    return TILE_LEVELS[-1]


def tiles_for_bbox(bbox: BBox, deg: float) -> list[tuple[int, int]]:
    """Integer ``(row, col)`` tile indices covering ``bbox``."""
    south, west, north, east = bbox
    row0, row1 = _floor(south, deg), _ceil(north, deg)
    col0, col1 = _floor(west, deg), _ceil(east, deg)
    return [
        (r, c)
        for r in range(row0, max(row1, row0 + 1))
        for c in range(col0, max(col1, col0 + 1))
    ]


def tile_bbox(tile: tuple[int, int], deg: float) -> BBox:
    """The bbox of one tile."""
    row, col = tile
    return (
        _round(row * deg), _round(col * deg),
        _round((row + 1) * deg), _round((col + 1) * deg),
    )

def tile_ref(tile: tuple[int, int], deg: float) -> str:
    """Stable key fragment for a tile. The level is included so the grids
    of different zoom levels can never collide."""
    row, col = tile
    return f"z{deg:g}/{row}/{col}"


def tile_of_point(lat: float, lon: float, deg: float) -> tuple[int, int]:
    return (_floor(lat, deg), _floor(lon, deg))


def covering_rects(
    tiles: set[tuple[int, int]], max_rects: int = 8
) -> list[tuple[int, int, int, int]]:
    """Decompose ``tiles`` into few ``(row0, col0, row1, col1)`` rectangles.

    Inclusive bounds. Missing tiles are usually a contiguous strip (a pan)
    or an L (a pan plus zoom), so a greedy row-run merge gets this to one or
    two rectangles and keeps the Overpass query small. Past ``max_rects`` the
    decomposition is abandoned for a single bounding rectangle — a slightly
    larger fetch beats a query with dozens of union members.
    """
    if not tiles:
        return []
    by_row: dict[int, list[int]] = {}
    for row, col in tiles:
        by_row.setdefault(row, []).append(col)

    # Maximal horizontal runs per row.
    runs: list[tuple[int, int, int]] = []  # (row, col0, col1)
    for row in sorted(by_row):
        cols = sorted(by_row[row])
        start = prev = cols[0]
        for col in cols[1:]:
            if col == prev + 1:
                prev = col
                continue
            runs.append((row, start, prev))
            start = prev = col
        runs.append((row, start, prev))

    # Merge runs with identical column spans in vertically adjacent rows.
    rects: list[list[int]] = []
    for row, col0, col1 in runs:
        for rect in rects:
            if rect[1] == col0 and rect[3] == col1 and rect[2] == row - 1:
                rect[2] = row
                break
        else:
            rects.append([row, col0, row, col1])

    if len(rects) > max_rects:
        rows = [t[0] for t in tiles]
        cols = [t[1] for t in tiles]
        return [(min(rows), min(cols), max(rows), max(cols))]
    return [(r[0], r[1], r[2], r[3]) for r in rects]


def rect_bbox(rect: tuple[int, int, int, int], deg: float) -> BBox:
    """The bbox of an inclusive ``(row0, col0, row1, col1)`` tile rectangle."""
    row0, col0, row1, col1 = rect
    return (
        _round(row0 * deg), _round(col0 * deg),
        _round((row1 + 1) * deg), _round((col1 + 1) * deg),
    )


def _index(elements: list[dict]) -> tuple[dict, dict, list[dict]]:
    """Split a flat element list into node coords, ways and relations."""
    nodes: dict[int, tuple[float, float]] = {}
    ways: dict[int, dict] = {}
    relations: list[dict] = []
    for el in elements:
        kind = el.get("type")
        if kind == "node":
            lat, lon = el.get("lat"), el.get("lon")
            if lat is not None and lon is not None:
                nodes[el["id"]] = (lat, lon)
        elif kind == "way":
            ways[el["id"]] = el
        elif kind == "relation":
            relations.append(el)
    return nodes, ways, relations


def _way_tiles(
    way: dict, nodes: dict[int, tuple[float, float]], deg: float
) -> set[tuple[int, int]]:
    """Tiles holding at least one of the way's nodes — Overpass's own rule."""
    out: set[tuple[int, int]] = set()
    for ref in way.get("nodes", []):
        coord = nodes.get(ref)
        if coord is not None:
            out.add(tile_of_point(coord[0], coord[1], deg))
    return out


def assign_to_tiles(
    elements: list[dict], deg: float, limit_to: set[tuple[int, int]] | None = None
) -> dict[tuple[int, int], list[dict]]:
    """Split an Overpass response into self-contained per-tile payloads.

    Each tile's payload is exactly what querying that tile alone would have
    returned: the ways with a node in it, every node those ways reference
    (including ones outside the tile), and the relations whose member ways
    land in it together with those members. ``limit_to`` restricts output to
    tiles actually wanted, so an over-fetched rectangle does not write keys
    for tiles nobody asked about.
    """
    nodes, ways, relations = _index(elements)
    out: dict[tuple[int, int], dict[tuple[str, int], dict]] = {}

    def bucket(tile: tuple[int, int]) -> dict[tuple[str, int], dict] | None:
        if limit_to is not None and tile not in limit_to:
            return None
        return out.setdefault(tile, {})

    def add_way(tile: tuple[int, int], way: dict) -> None:
        buf = bucket(tile)
        if buf is None:
            return
        buf[("way", way["id"])] = way
        for ref in way.get("nodes", []):
            coord = nodes.get(ref)
            if coord is not None:
                buf[("node", ref)] = {
                    "type": "node", "id": ref, "lat": coord[0], "lon": coord[1],
                }

    way_tiles: dict[int, set[tuple[int, int]]] = {}
    for wid, way in ways.items():
        hit = _way_tiles(way, nodes, deg)
        way_tiles[wid] = hit
        for tile in hit:
            add_way(tile, way)

    # A relation belongs wherever its members do, and `>` would have pulled
    # every member way in with it, so they travel together.
    for rel in relations:
        member_ids = [
            m["ref"] for m in rel.get("members", [])
            if m.get("type") == "way" and m.get("ref") in ways
        ]
        hit: set[tuple[int, int]] = set()
        for ref in member_ids:
            hit |= way_tiles.get(ref, set())
        for tile in hit:
            buf = bucket(tile)
            if buf is None:
                continue
            buf[("relation", rel["id"])] = rel
            for ref in member_ids:
                add_way(tile, ways[ref])

    # Sorted so a tile's payload is byte-stable no matter which fetch
    # produced it. Assembly order decides the order of <path> elements in
    # the SVG, and a render must not depend on which tiles happened to be
    # cached when it ran.
    return {
        tile: [buf[k] for k in sorted(buf)]
        for tile, buf in out.items()
    }


def clip_elements(elements: list[dict], bbox: BBox) -> list[dict]:
    """Reduce merged tiles to exactly what one query over ``bbox`` returns.

    Tiles cover the bbox but overhang it, and a way is kept whole by
    whichever tile holds one of its nodes. Without this the element set —
    and therefore the renderer's extent — would depend on the tile level.
    Applies the same node-membership rule Overpass uses.
    """
    south, west, north, east = bbox
    nodes, ways, relations = _index(elements)

    def inside(ref: int) -> bool:
        coord = nodes.get(ref)
        if coord is None:
            return False
        lat, lon = coord
        return south <= lat <= north and west <= lon <= east

    kept: dict[tuple[str, int], dict] = {}

    def keep_way(way: dict) -> None:
        kept[("way", way["id"])] = way
        for ref in way.get("nodes", []):
            coord = nodes.get(ref)
            if coord is not None:
                kept[("node", ref)] = {
                    "type": "node", "id": ref, "lat": coord[0], "lon": coord[1],
                }

    selected: set[int] = set()
    for wid, way in ways.items():
        if any(inside(ref) for ref in way.get("nodes", [])):
            selected.add(wid)
            keep_way(way)

    for rel in relations:
        member_ids = [
            m["ref"] for m in rel.get("members", [])
            if m.get("type") == "way" and m.get("ref") in ways
        ]
        if not any(ref in selected for ref in member_ids):
            continue
        kept[("relation", rel["id"])] = rel
        for ref in member_ids:
            keep_way(ways[ref])

    return list(kept.values())


__all__ = [
    "TILE_LEVELS",
    "assign_to_tiles",
    "clip_elements",
    "covering_rects",
    "rect_bbox",
    "snap_bbox",
    "tile_bbox",
    "tile_deg_for_bbox",
    "tile_of_point",
    "tile_ref",
    "tiles_for_bbox",
]
