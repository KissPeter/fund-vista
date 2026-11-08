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
    cache_set,
    cache_set_many,
)
from backend.airports.config import settings
from backend.airports.overpass import CONTEXT_CLASSES
from backend.airports.render import render_source_version
from backend.airports.schemas import RenderRequest
from backend.airports.sources import OVERTURE_BACKOFF_S, SOURCES_VERSION

# P5: context split outputs are cached independently of the render params
# (width/minlen/zoom/layers/labels all render from the same context rings).
# Keys mirror the Overpass payload key (lat/lon/radius). The airfield core
# is no longer split here: its geometry is cached as an AirfieldResult
# (see backend.airports.fetching). Bump the version when split_context
# semantics change.
SPLIT_VERSION = "split-v1"


def _ctx_split_key(lat: float, lon: float, radius_m: float) -> str:
    center = f"{lat:.5f},{lon:.5f}"
    radius = f"r={radius_m:.0f}"
    return airports_cache_key("split-ctx", center, radius, SPLIT_VERSION)


def _unjson_geoms(geoms: dict, classes: list[str]) -> dict[str, list[list[tuple[float, float]]]]:
    return {
        cls: [[tuple(pt) for pt in pl] for pl in geoms[cls]]
        for cls in classes
    }


async def _load_cached_splits(
    ctx_key: str, wants_context: bool, warnings: list[str],
) -> dict | None:
    """Cached context geoms, or None when absent (or not wanted)."""
    if not wants_context:
        return None
    found = await cache_get_many([ctx_key])
    ctx_json = found.get(ctx_key)
    if ctx_json is None:
        return None
    try:
        ctx_geoms = _unjson_geoms(json.loads(ctx_json)["geoms"], CONTEXT_CLASSES)
    except (ValueError, KeyError, TypeError):
        return None
    warnings.append("context_split_cache_hit")
    return ctx_geoms


async def _store_splits(
    ctx_key: str,
    ctx: dict[str, list[list[tuple[float, float]]]] | None,
) -> None:
    if ctx is not None:
        await cache_set_many({
            ctx_key: json.dumps({"geoms": ctx}, separators=(",", ":")),
        })


def _render_keys(icao: str, radius_m: float, body: RenderRequest) -> tuple[str, str]:
    """``(svg key, metadata key)`` for one diagram.

    The metadata sidecar holds the counts and rotation the response
    reports. Without it an SVG hit had to re-fetch the Overpass payload and
    run a full ``render_diagram`` just to recover ``rotation``, throwing the
    rendered document away — the cache hit cost as much as a miss.

    The airfield source is part of the key: toggling Overture, pinning
    another release or bumping ``SOURCES_VERSION`` re-keys every diagram.
    ``render_source_version`` only hashes ``render.py``, so it cannot see
    those changes on its own.
    """
    parts = (
        icao, f"r={radius_m:.0f}",
        f"minlen={body.min_path_len_m}", f"width={body.width}",
        f"layers={','.join(sorted(body.effective_layers()))}",
        f"zoom={body.zoom}",
        f"tlabels={int(body.taxiway_labels)}",
        f"v={render_source_version()}",
        f"overture={int(settings.overture_enabled)}",
        f"release={settings.overture_release}",
        f"sources={SOURCES_VERSION}",
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


async def _store_degraded_svg(svg_key: str, svg_text: str) -> None:
    """Keep a degraded render's SVG for the ``svg_url`` link, briefly.

    Only the SVG is written, with the Overture backoff as its TTL and no
    metadata sidecar. ``get_result`` reads just the SVG key, so the link
    resolves; ``_load_cached_render`` needs both keys, so the next render
    request rebuilds instead of serving the degraded result as a hit.
    """
    await cache_set(svg_key, svg_text, ttl_s=int(OVERTURE_BACKOFF_S))


__all__ = [
    "SPLIT_VERSION",
    "_ctx_split_key",
    "_load_cached_render",
    "_load_cached_splits",
    "_render_keys",
    "_store_degraded_svg",
    "_store_render",
    "_store_splits",
    "_unjson_geoms",
]
