"""Upstream fetch helpers for /v1/citymap (REF-002 Phase 2).

Pure move from :mod:`backend.citymap.router` — identical behavior, no
functional change. This module owns the tile-granular OSM fetch path
(Overpass with OSM Main API fallback, per-layer split, clipped assembly);
the router keeps the endpoints and the fetch-render-store orchestration.
"""

from __future__ import annotations

import json
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from backend.cancel import race_cancel
from backend.citymap.cache import cache_get_many, cache_set_many
from backend.citymap.config import settings
from backend.citymap.layers import LAYER_ORDER
from backend.citymap.osm_api import OsmApiError, fetch_osm_api, merge_elements
from backend.citymap.overpass import (
    BBox,
    OverpassError,
    fetch_overpass,
    match_relation_layer,
    match_way_layer,
    union_members,
    wrap_query,
)
from backend.citymap.render_cache import _tile_key
from backend.citymap.tiles import (
    assign_to_tiles,
    clip_elements,
    covering_rects,
    rect_bbox,
    tile_bbox,
    tile_deg_for_bbox,
    tiles_for_bbox,
)
from backend.http import error_response

log = logging.getLogger(__name__)


# Cheap, structural layers first; the heavy ``buildings`` layer last so a
# timeout on it costs only itself, never the roads already fetched.
_FETCH_ORDER: tuple[str, ...] = tuple(
    [name for name in LAYER_ORDER if name != "buildings"] + ["buildings"]
)


def _overlaps(a: BBox, b: BBox) -> bool:
    """Whether two ``(south, west, north, east)`` boxes share area."""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


async def _fetch_missing(
    missing: dict[str, set[tuple[int, int]]], deg: float, warnings: list[str],
    request: Request | None = None,
    endpoint: str = "/v1/citymap/render",
    started_mono: float = 0.0,
) -> tuple[
    dict[tuple[str, tuple[int, int]], list[dict]], list[str], JSONResponse | None
]:
    """Fetch the missing ``(layer -> tiles)`` work one layer at a time.

    Layers are fetched in :data:`_FETCH_ORDER` and each layer's tiles are
    cached the moment they arrive, so the map builds up layer by layer: a
    failure (or client abort) on a later layer never discards the earlier
    ones, and a retry only asks for what is still missing.

    P2: every upstream fetch is raced against a disconnect watcher
    (``race_cancel``) — on abort the fetch task is cancelled and 499 raised;
    layers stored before the abort stay cached (each is complete on its own).

    Returns ``(payloads for the requested missing tiles, incomplete layers,
    error)``. A layer is incomplete when its fetch failed outright or the OSM
    API fallback lost some cells. Tiles the fetch did not fully cover are
    NEVER cached — caching them as empty would pin a blank area as a real
    answer. The error is set only when no layer could be fetched at all.
    """
    payloads: dict[tuple[str, tuple[int, int]], list[dict]] = {}
    incomplete: list[str] = []
    errors: list[JSONResponse] = []
    fetched_any = False
    # The OSM Main API is not layer-aware: read it once for every missing
    # tile and let each layer filter, instead of N reads of the same cells.
    osm_all: tuple[list[dict], list[BBox]] | OsmApiError | None = None

    for layer in (name for name in _FETCH_ORDER if name in missing):
        rects = covering_rects(missing[layer])
        covered: set[tuple[int, int]] = {
            (r, c)
            for row0, col0, row1, col1 in rects
            for r in range(row0, row1 + 1)
            for c in range(col0, col1 + 1)
        }
        members: list[str] = []
        for rect in rects:
            members.extend(union_members(rect_bbox(rect, deg), [layer]))
        failed_cells: list[BBox] = []
        try:
            # Checkpoint 2/3 boundary: race the Overpass fetch so abort stops
            # the httpx wait instead of running to completion (P2.3).
            elements = await race_cancel(
                request, fetch_overpass(wrap_query(members)),
                endpoint=endpoint, stage="overpass", started_mono=started_mono,
            )
        except OverpassError as exc:
            log.warning(
                "citymap overpass failed for %s, trying OSM API: %s", layer, exc.detail
            )
            if osm_all is None:
                all_tiles: set[tuple[int, int]] = set()
                for tiles in missing.values():
                    all_tiles |= tiles
                chunks: list[list[dict]] = []
                cells: list[BBox] = []
                try:
                    for rect in covering_rects(all_tiles):
                        got = await race_cancel(
                            request, fetch_osm_api(rect_bbox(rect, deg)),
                            endpoint=endpoint, stage="overpass",
                            started_mono=started_mono,
                        )
                        cells.extend(getattr(got, "failed_cells", ()))
                        chunks.append(got)
                    osm_all = (merge_elements(chunks), cells)
                except OsmApiError as exc2:
                    osm_all = exc2
            if isinstance(osm_all, OsmApiError):
                errors.append(error_response(
                    502,
                    "overpass_unavailable",
                    f"Map data fetch failed (Overpass: {exc.detail}; "
                    f"OSM API: {osm_all.detail}). "
                    "(Upstreams busy — retry in a minute, with fewer layers or a smaller area).",
                ))
                incomplete.append(layer)
                continue
            elements, failed_cells = osm_all
            if "osm_api_fallback" not in warnings:
                warnings.append("osm_api_fallback")
            covered = {
                t for t in covered
                if not any(_overlaps(tile_bbox(t, deg), cell) for cell in failed_cells)
            }

        fetched_any = True
        if not missing[layer] <= covered:
            incomplete.append(layer)
        # A way straddling the edge of the fetched region would otherwise be
        # filed under an outside tile; limit_to keeps only covered tiles.
        by_tile = assign_to_tiles(
            _select_layer(elements, layer), deg, limit_to=covered
        )
        # Cache every tile the fetch covered, not just the ones this request
        # lacked — the rectangle is already paid for and the neighbours are
        # what the next pan will ask for. A covered tile with no features of
        # this layer is a real answer, not a miss.
        writes = {
            _tile_key(tile, deg, layer): json.dumps(by_tile.get(tile, []))
            for tile in covered
        }
        await cache_set_many(writes, settings.tile_cache_ttl_hours * 3600)
        for tile in missing[layer] & covered:
            payloads[(layer, tile)] = by_tile.get(tile, [])

    if not fetched_any and errors:
        return payloads, incomplete, errors[0]
    return payloads, incomplete, None


def _select_layer(elements: list[dict], layer: str) -> list[dict]:
    """Elements belonging to ``layer``, plus the nodes they reference.

    A union query returns every layer at once; each tile key must hold only
    its own layer or the per-layer reuse this whole scheme buys would be a
    lie. Uses the same predicates the renderer does.
    """
    nodes: dict[int, dict] = {}
    ways: dict[int, dict] = {}
    relations: list[dict] = []
    for el in elements:
        kind = el.get("type")
        if kind == "node":
            nodes[el["id"]] = el
        elif kind == "way":
            ways[el["id"]] = el
        elif kind == "relation":
            relations.append(el)

    only = [layer]
    kept: dict[tuple[str, int], dict] = {}

    def keep_way(way: dict) -> None:
        kept[("way", way["id"])] = way
        for ref in way.get("nodes", []):
            node = nodes.get(ref)
            if node is not None:
                kept[("node", ref)] = node

    for rel in relations:
        if match_relation_layer(rel.get("tags", {}) or {}, only) is None:
            continue
        kept[("relation", rel["id"])] = rel
        for member in rel.get("members", []):
            if member.get("type") == "way" and member.get("ref") in ways:
                keep_way(ways[member["ref"]])
    for way in ways.values():
        if match_way_layer(way.get("tags", {}) or {}, only) is not None:
            keep_way(way)
    return list(kept.values())


async def _load_raw(
    bbox: BBox, layers: list[str], warnings: list[str],
    request: Request | None = None,
    endpoint: str = "/v1/citymap/render",
    started_mono: float = 0.0,
) -> tuple[list[dict] | None, JSONResponse | None]:
    """Tile-cached fetch with OSM Main API fallback.

    Raw OSM is held per ``(tile, layer)`` rather than per
    ``(exact bbox, layer set)``, so a pan refetches only the new tiles and
    ticking a layer on reuses the layers already held. The result is
    clipped back to ``bbox`` so the element set — and therefore the
    renderer's extent — is identical to what one query over ``bbox`` would
    have produced, whatever the cache happened to hold.

    Returns (elements, None) or (None, error response) when Overpass *and*
    the OSM Main API both fail. Fallback hits append ``osm_api_fallback``
    so clients can tell the data came from chunked ``/api/0.6/map`` reads.
    """
    deg = tile_deg_for_bbox(bbox, settings.tile_max_tiles)
    tiles = tiles_for_bbox(bbox, deg)

    keys = {
        (layer, tile): _tile_key(tile, deg, layer)
        for layer in layers
        for tile in tiles
    }
    found = await cache_get_many(list(keys.values()))

    payloads: dict[tuple[str, tuple[int, int]], list[dict]] = {}
    missing: dict[str, set[tuple[int, int]]] = {}
    for (layer, tile), key in keys.items():
        raw = found.get(key)
        if raw is not None:
            try:
                payloads[(layer, tile)] = json.loads(raw)
                continue
            except ValueError:
                pass
        missing.setdefault(layer, set()).add(tile)

    hit_count = len(keys) - sum(len(v) for v in missing.values())
    if hit_count:
        warnings.append("overpass_cache_hit")

    if missing:
        fetched, incomplete, err = await _fetch_missing(
            missing, deg, warnings,
            request=request, endpoint=endpoint, started_mono=started_mono,
        )
        if err is not None:
            return None, err
        payloads.update(fetched)
        if incomplete:
            # Some layers/areas are blank because upstream failed, not
            # because the map is empty: flag it so callers neither cache the
            # finished SVG nor present it as final.
            warnings.append("partial_data")
            warnings.extend(f"layers_pending:{name}" for name in incomplete)

    # Assemble in a fixed (layer, tile) order rather than in whatever order
    # the cache answered. Assembly order decides the order of <path>
    # elements in the SVG, and the same request must render identically
    # whether it was served entirely from cache or partly refetched.
    chunks = [
        payloads.get((layer, tile), [])
        for layer in layers
        for tile in tiles
    ]
    log.info(
        "citymap.tiles z=%g tiles=%d layers=%d hit=%d miss=%d",
        deg, len(tiles), len(layers), hit_count,
        sum(len(v) for v in missing.values()),
    )
    return clip_elements(merge_elements(chunks), bbox), None


__all__ = ["_fetch_missing", "_load_raw", "_select_layer"]
