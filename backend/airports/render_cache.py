"""Split + render cache helpers for /v1/airports (REF-002 Phase 2).

Pure move from :mod:`backend.airports.router` — identical behavior, no
functional change. This module owns the split/render key builders and the
cache load/store pairs (split outputs, rendered SVGs + metadata sidecars).
"""

from __future__ import annotations

import json

from backend.airports.cache import (
    airports_cache_key,
    cache_get_many,
    cache_set_many,
)
from backend.airports.overpass import AEROWAY_CLASSES, CONTEXT_CLASSES
from backend.airports.render import render_source_version
from backend.airports.schemas import RenderRequest

# P5: split outputs are cached independently of the render params (width/
# minlen/zoom/layers/labels all render from the same aeroway/context rings).
# Keys mirror the Overpass payload key (lat/lon/radius) so a slider change
# with the same airport reuses the split and skips the per-render CPU. Bump
# the version when split_aeroway/split_context semantics change.
SPLIT_VERSION = "split-v1"


def _split_keys(lat: float, lon: float, radius_m: float) -> tuple[str, str]:
    center = f"{lat:.5f},{lon:.5f}"
    radius = f"r={radius_m:.0f}"
    return (
        airports_cache_key("split-aeroway", center, radius, SPLIT_VERSION),
        airports_cache_key("split-ctx", center, radius, SPLIT_VERSION),
    )


def _unjson_geoms(geoms: dict, classes: list[str]) -> dict[str, list[list[tuple[float, float]]]]:
    return {
        cls: [[tuple(pt) for pt in pl] for pl in geoms[cls]]
        for cls in classes
    }


async def _load_cached_splits(
    aer_key: str, ctx_key: str, wants_context: bool, warnings: list[str],
) -> tuple[dict | None, dict | None, dict | None]:
    """Cached ``(aeroway geoms, raw_counts, context geoms)`` or None entries."""
    keys = [aer_key]
    if wants_context:
        keys.append(ctx_key)
    found = await cache_get_many(keys)
    aer_geoms = raw_counts = ctx_geoms = None
    aer_json = found.get(aer_key)
    if aer_json is not None:
        try:
            data = json.loads(aer_json)
            aer_geoms = _unjson_geoms(data["geoms"], AEROWAY_CLASSES)
            raw_counts = data["raw_counts"]
        except (ValueError, KeyError, TypeError):
            aer_geoms = raw_counts = None
    if aer_geoms is not None:
        warnings.append("aeroway_split_cache_hit")
    if wants_context:
        ctx_json = found.get(ctx_key)
        if ctx_json is not None:
            try:
                ctx_geoms = _unjson_geoms(json.loads(ctx_json)["geoms"], CONTEXT_CLASSES)
            except (ValueError, KeyError, TypeError):
                ctx_geoms = None
        if ctx_geoms is not None:
            warnings.append("context_split_cache_hit")
    return aer_geoms, raw_counts, ctx_geoms


async def _store_splits(
    aer_key: str, ctx_key: str,
    aer: tuple[dict[str, list[list[tuple[float, float]]]], dict[str, int]] | None,
    ctx: dict[str, list[list[tuple[float, float]]]] | None,
) -> None:
    items: dict[str, str] = {}
    if aer is not None:
        geoms, raw_counts = aer
        items[aer_key] = json.dumps(
            {"geoms": geoms, "raw_counts": raw_counts}, separators=(",", ":")
        )
    if ctx is not None:
        items[ctx_key] = json.dumps({"geoms": ctx}, separators=(",", ":"))
    if items:
        await cache_set_many(items)


def _render_keys(icao: str, radius_m: float, body: RenderRequest) -> tuple[str, str]:
    """``(svg key, metadata key)`` for one diagram.

    The metadata sidecar holds the counts and rotation the response
    reports. Without it an SVG hit had to re-fetch the Overpass payload and
    run a full ``render_diagram`` just to recover ``rotation``, throwing the
    rendered document away — the cache hit cost as much as a miss.
    """
    parts = (
        icao, f"r={radius_m:.0f}",
        f"minlen={body.min_path_len_m}", f"width={body.width}",
        f"layers={','.join(sorted(body.effective_layers()))}",
        f"zoom={body.zoom}",
        f"tlabels={int(body.taxiway_labels)}",
        f"v={render_source_version()}",
    )
    return airports_cache_key("svg", *parts), airports_cache_key("meta", *parts)


async def _load_cached_render(
    svg_key: str, meta_key: str, warnings: list[str]
) -> tuple[str, dict[str, int], dict[str, int], float] | None:
    """Cached SVG plus its metadata, or None when either is missing.

    Both are required: the response cannot be built from the document
    alone, and re-deriving the metadata costs a full render. A hit on the
    SVG with no sidecar is therefore treated as a miss.
    """
    found = await cache_get_many([svg_key, meta_key])
    svg_text, meta_json = found.get(svg_key), found.get(meta_key)
    if svg_text is None or meta_json is None:
        return None
    try:
        meta = json.loads(meta_json)
        result = (
            svg_text, meta["path_counts"], meta["raw_counts"], meta["rotation"],
        )
    except (ValueError, KeyError):
        return None
    warnings.append("svg_cache_hit")
    return result


async def _store_render(
    svg_key: str, meta_key: str, svg_text: str,
    path_counts: dict[str, int], raw_counts: dict[str, int], rotation: float,
) -> None:
    """Store the SVG and its metadata sidecar in one pipelined write."""
    await cache_set_many({
        svg_key: svg_text,
        meta_key: json.dumps({
            "path_counts": path_counts,
            "raw_counts": raw_counts,
            "rotation": rotation,
        }),
    })


__all__ = [
    "SPLIT_VERSION",
    "_load_cached_render",
    "_load_cached_splits",
    "_render_keys",
    "_split_keys",
    "_store_render",
    "_store_splits",
    "_unjson_geoms",
]
