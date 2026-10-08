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
    tile_deg_for_bbox,
    tiles_for_bbox,
)
from backend.http import error_response

log = logging.getLogger(__name__)


async def _fetch_missing(
    missing: dict[str, set[tuple[int, int]]], deg: float, warnings: list[str],
    request: Request | None = None,
    endpoint: str = "/v1/citymap/render",
    started_mono: float = 0.0,
) -> tuple[dict[str, list[dict]] | None, set[tuple[int, int]], JSONResponse | None]:
    """Fetch the missing ``(layer -> tiles)`` work in as few queries as we can.

    Missing tiles are decomposed into rectangles and unioned into a single
    Overpass query, so a cold render still costs one round-trip exactly as
    it used to — only a warm one gets cheaper.

    P2: the Overpass ``httpx`` fetch is raced against a disconnect watcher
    (``race_cancel``) — on abort the fetch task is cancelled and 499 raised
    with no partial cache write.

    Returns ``(elements per layer, tiles fully covered by the fetch, error)``.
    The covered set matters: a way whose nodes straddle the edge of the
    fetched region would otherwise be filed under an outside tile, and
    caching that tile would pin a payload missing everything else there.
    """
    all_tiles: set[tuple[int, int]] = set()
    for tiles in missing.values():
        all_tiles |= tiles
    rects = covering_rects(all_tiles)
    layers = sorted(missing)

    covered: set[tuple[int, int]] = set()
    for row0, col0, row1, col1 in rects:
        for r in range(row0, row1 + 1):
            for c in range(col0, col1 + 1):
                covered.add((r, c))

    members: list[str] = []
    for rect in rects:
        members.extend(union_members(rect_bbox(rect, deg), layers))
    try:
        # Checkpoint 2/3 boundary: race the Overpass fetch so abort stops
        # the httpx wait instead of running to completion (P2.3).
        elements = await race_cancel(
            request, fetch_overpass(wrap_query(members)),
            endpoint=endpoint, stage="overpass", started_mono=started_mono,
        )
    except OverpassError as exc:
        log.warning("citymap overpass failed, trying OSM API: %s", exc.detail)
        chunks: list[list[dict]] = []
        try:
            for rect in rects:
                chunks.append(await race_cancel(
                    request, fetch_osm_api(rect_bbox(rect, deg)),
                    endpoint=endpoint, stage="overpass",
                    started_mono=started_mono,
                ))
        except OsmApiError as exc2:
            return None, set(), error_response(
                502,
                "overpass_unavailable",
                f"Map data fetch failed (Overpass: {exc.detail}; "
                f"OSM API: {exc2.detail}). "
                "(Upstreams busy — retry in a minute, with fewer layers or a smaller area).",
            )
        elements = merge_elements(chunks)
        warnings.append("osm_api_fallback")

    # One query returns every layer at once, and the OSM Main API fallback
    # is not layer-aware at all, so each tile key gets only its own layer —
    # otherwise the per-layer reuse this scheme buys would be a lie.
    per_layer = {layer: _select_layer(elements, layer) for layer in layers}
    return per_layer, covered, None


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
        per_layer, covered, err = await _fetch_missing(
            missing, deg, warnings,
            request=request, endpoint=endpoint, started_mono=started_mono,
        )
        if err is not None:
            return None, err
        assert per_layer is not None
        writes: dict[str, str] = {}
        for layer, elements in per_layer.items():
            by_tile = assign_to_tiles(elements, deg, limit_to=covered)
            # Cache every tile the fetch covered, not just the ones this
            # request lacked — the rectangle is already paid for and the
            # neighbours are what the next pan will ask for. A covered tile
            # with no features of this layer is a real answer, not a miss;
            # storing the empty list stops it being re-fetched forever.
            for tile in covered:
                payload = by_tile.get(tile, [])
                writes[_tile_key(tile, deg, layer)] = json.dumps(payload)
                if tile in missing[layer]:
                    payloads[(layer, tile)] = payload
        await cache_set_many(writes, settings.tile_cache_ttl_hours * 3600)

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
