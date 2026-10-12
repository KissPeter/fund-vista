"""Render cache helpers for /v1/traffic (Phase 2).

L3 (basemap, per layer over a quantised view, 30 d) lives in
:mod:`backend.traffic.basemap`; this module owns the L4 rendered-SVG keys
plus the SVG + metadata-sidecar load/store pair, mirroring
:mod:`backend.airports.render_cache`.

A hit needs both the SVG and its sidecar (counts live only in the
sidecar — re-deriving them costs a full render, so SVG-without-sidecar
is treated as a miss).
"""

from __future__ import annotations

import json

from backend.traffic import cache as _cache
from backend.traffic.cache import (
    traffic_cache_key,
)
from backend.traffic.classify import CLASSIFIER_VERSION
from backend.traffic.render import RENDER_VERSION


def _filter_fingerprint(body) -> str:
    return "|".join([
        f"arr={int(body.arrivals)}", f"dep={int(body.departures)}",
        f"hold={int(body.holding)}", f"ga={int(body.go_around)}",
        "types=" + ",".join(sorted(body.types)),
        "airlines=" + ",".join(sorted(a.upper() for a in body.airlines)),
        "cs=" + body.callsign.upper(),
        f"mil={int(body.exclude_military)}", f"priv={int(body.exclude_privacy)}",
    ])


def _render_keys(icao: str, day_iso: str, t_from: str, t_to: str,
                 view: tuple[float, float, float, float], body) -> tuple[str, str]:
    """``(svg key, metadata key)`` for one traffic render (L4, plan §6.3).

    Filters, window and view are all in the key — changing a filter hits
    L2 + L3 and re-renders without touching the archive. Classifier and
    renderer versions are in the key so heuristic/render fixes invalidate.
    """
    s, n, w, e = view
    parts = (
        icao, day_iso, f"{t_from}-{t_to}",
        "view=%.5f,%.5f,%.5f,%.5f" % (s, n, w, e),
        _filter_fingerprint(body),
        f"width={body.width}",
        f"classifier={CLASSIFIER_VERSION}", f"renderer={RENDER_VERSION}",
    )
    return traffic_cache_key("svg", *parts), traffic_cache_key("svgmeta", *parts)


def _svg_token(svg_key: str) -> str:
    return svg_key.rsplit(":", 1)[-1]


def _svg_lookup_key(token: str) -> str:
    return f"{_cache.KEY_PREFIX}:svg:{token.lower()}"


async def _load_cached_render(
    svg_key: str, meta_key: str, warnings: list[str],
) -> tuple[str, dict[str, int], int] | None:
    """Cached ``(svg_text, counts, movements)`` or None when either is missing."""
    found = await _cache.cache_get_many([svg_key, meta_key])
    svg_text, meta_json = found.get(svg_key), found.get(meta_key)
    if svg_text is None or meta_json is None:
        return None
    try:
        meta = json.loads(meta_json)
        result = (svg_text, meta["counts"], meta["movements"])
    except (ValueError, KeyError):
        return None
    warnings.append("svg_cache_hit")
    return result


async def _store_render(
    svg_key: str, meta_key: str, svg_text: str,
    counts: dict[str, int], movements: int, ttl_s: int | None = None,
) -> None:
    """Store the SVG and its metadata sidecar (L4 TTL, plan §6.3)."""
    if ttl_s is None:
        from backend.traffic.config import settings

        ttl_s = settings.svg_ttl_hours * 3600
    await _cache.cache_set_many({
        svg_key: svg_text,
        meta_key: json.dumps({"counts": counts, "movements": movements}),
    }, ttl_s=ttl_s)


async def _load_result_svg(token: str) -> str | None:
    if len(token) != 40 or any(c not in "0123456789abcdef" for c in token.lower()):
        return None
    return await _cache.cache_get(_svg_lookup_key(token))


__all__ = [
    "_filter_fingerprint",
    "_load_cached_render",
    "_load_result_svg",
    "_render_keys",
    "_store_render",
    "_svg_lookup_key",
    "_svg_token",
]
