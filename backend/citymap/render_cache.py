"""Split + render cache helpers for /v1/citymap (REF-002 Phase 2).

Pure move from :mod:`backend.citymap.router` — identical behavior, no
functional change. The router keeps the endpoints; this module owns the
key builders and the cache load/store pairs (split outputs, raw tiles
via :func:`_tile_key`, rendered SVGs + counts sidecars).
"""

from __future__ import annotations

import json

from backend.citymap.cache import (
    cache_get,
    cache_get_many,
    cache_set,
    cache_set_many,
    citymap_cache_key,
)
from backend.citymap.overpass import BBox, bbox_str
from backend.citymap.render import render_source_version
from backend.citymap.schemas import RenderRequest
from backend.citymap.tiles import tile_ref


def _tile_key(tile: tuple[int, int], deg: float, layer: str) -> str:
    return citymap_cache_key("tile", tile_ref(tile, deg), layer)


# P5: split_elements output is cached separately from the rendered SVG. The
# SVG cache key includes width/min_path_len, so sliding those controls with
# the same area+layers re-renders from the tiles every time; the split is
# independent of both, so a hit skips ``_load_raw`` and ``split_elements``
# entirely. The key binds the exact (bbox, layer set) — adding a layer can
# reassign a way to a different layer via the ordering in match_way_layer, so
# per-layer keys would be unsound. Bump the version when split semantics or
# render geometry change.
SPLIT_VERSION = "split-v1"


def _split_key(bbox: BBox, layers: list[str]) -> str:
    return citymap_cache_key(
        "split", bbox_str(bbox), ",".join(sorted(layers)), SPLIT_VERSION
    )


async def _load_cached_split(
    bbox: BBox, layers: list[str], warnings: list[str],
) -> tuple[dict[str, list[list[tuple[float, float]]]], dict[str, int]] | None:
    key = _split_key(bbox, layers)
    raw = await cache_get(key)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        geoms = {
            layer: [
                [tuple(pt) for pt in pl]
                for pl in data["geoms"][layer]
            ]
            for layer in layers
        }
        raw_counts = dict(data["raw_counts"])
    except (ValueError, KeyError, TypeError):
        return None
    warnings.append("split_cache_hit")
    return geoms, raw_counts


async def _store_split(
    bbox: BBox, layers: list[str],
    geoms: dict[str, list[list[tuple[float, float]]]],
    raw_counts: dict[str, int],
) -> None:
    await cache_set(
        _split_key(bbox, layers),
        json.dumps({"geoms": geoms, "raw_counts": raw_counts}, separators=(",", ":")),
    )


def _render_keys(
    bbox: BBox, layers: list[str], body: RenderRequest
) -> tuple[str, str]:
    """``(svg key, counts key)`` for one render.

    The counts sidecar holds the path/raw totals the response reports. It
    exists so an SVG hit costs two small reads instead of re-parsing a
    multi-MB payload and re-running ``split_elements`` purely to fill in
    numbers the renderer already computed once.

    The renderer source version rides every key (same convention as the
    airports diagram version): a framing change must never keep serving
    art drawn under the old geometry.
    """
    parts = (
        render_source_version(),
        bbox_str(bbox), ",".join(sorted(layers)),
        f"minlen={body.min_path_len_m}", f"width={body.width}",
    )
    # Bearing/aspect join the key only when rotation is active, so the
    # overwhelming north-up traffic keeps its existing cache entries.
    bearing = (body.bearing_deg or 0.0) % 360.0
    if not (bearing < 1e-9 or bearing > 360.0 - 1e-9):
        parts += (f"bearing={bearing:.2f}", f"aspect={(body.viewport_aspect or 0.0):.4f}")
    return citymap_cache_key("svg", *parts), citymap_cache_key("counts", *parts)


async def _load_cached_render(
    svg_key: str, counts_key: str, layers: list[str], warnings: list[str]
) -> tuple[str, dict[str, int], dict[str, int]] | None:
    """Cached SVG plus its counts, or None when the SVG is not held."""
    found = await cache_get_many([svg_key, counts_key])
    svg_text = found.get(svg_key)
    if svg_text is None:
        return None
    warnings.append("svg_cache_hit")
    raw_counts: dict[str, int] = {"nodes": -1, "ways": -1, "relations": -1}
    counts_json = found.get(counts_key)
    if counts_json is not None:
        try:
            meta = json.loads(counts_json)
            return svg_text, meta["path_counts"], meta["raw_counts"]
        except (ValueError, KeyError):
            pass
    # Sidecar missing or unreadable (an SVG cached before this existed, or
    # an expiry race): count paths in the document rather than re-fetching.
    warnings.append("counts_from_cached_svg")
    return svg_text, _count_paths_in_svg(svg_text, layers), raw_counts


async def _store_render(
    svg_key: str, counts_key: str, svg_text: str,
    path_counts: dict[str, int], raw_counts: dict[str, int],
) -> None:
    """Store the SVG and its counts sidecar in one pipelined write."""
    await cache_set_many({
        svg_key: svg_text,
        counts_key: json.dumps(
            {"path_counts": path_counts, "raw_counts": raw_counts}
        ),
    })


def _count_paths_in_svg(svg_text: str, layers: list[str]) -> dict[str, int]:
    """Fallback path counter for the cached-SVG-only branch."""
    counts = {layer: 0 for layer in layers}
    for layer in layers:
        marker = f'id="citymap-{layer}"'
        start = svg_text.find(marker)
        if start == -1:
            continue
        group_end = svg_text.find("</g>", start)
        segment = svg_text[start:group_end] if group_end != -1 else svg_text[start:]
        counts[layer] = segment.count("<path")
    return counts


__all__ = [
    "SPLIT_VERSION",
    "_count_paths_in_svg",
    "_load_cached_render",
    "_load_cached_split",
    "_render_keys",
    "_split_key",
    "_store_render",
    "_store_split",
    "_tile_key",
]
