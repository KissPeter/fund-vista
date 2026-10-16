"""Place-name resolution via Nominatim (the city-roads approach).

``geocode_city`` mirrors what city-roads' ``findBoundaryByName`` does for
the search box: ask Nominatim for the best match and take its bounding
box. Results are cached in Redis for ``cache_ttl_hours`` — every cache
hit avoids the upstream entirely. Cache misses are paced process-wide to
the Nominatim usage policy (max 1 req/s, identifying User-Agent + Referer)
via :func:`_throttle_nominatim` before any HTTP request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from backend.citymap.cache import cache_get, cache_set, citymap_cache_key
from backend.citymap.config import settings
from backend.citymap.overpass import BBox

log = logging.getLogger(__name__)

# Nominatim usage policy: at most 1 request/s against the public instance.
# Process-wide throttle enforced on every cache miss (hits never reach the
# upstream). State is module-level so concurrent geocode/search calls share
# one clock; tests manipulate it via _reset_nominatim_throttle() instead of
# sleeping (see test_data_provider_terms.py).
_nominatim_lock = asyncio.Lock()
_last_nominatim_mono: float = 0.0  # 0.0 = no request yet this process


def _reset_nominatim_throttle() -> None:
    """Forget the last upstream timestamp (tests only)."""
    global _last_nominatim_mono
    _last_nominatim_mono = 0.0


async def _throttle_nominatim() -> float:
    """Wait until ``nominatim_min_interval_s`` elapsed since the last call.

    Returns the seconds waited (0.0 when no wait was needed) and stamps the
    clock. The stamp is taken under the lock so two concurrent callers are
    serialized into compliant 1-second spacing.
    """
    global _last_nominatim_mono
    async with _nominatim_lock:
        interval = max(0.0, float(settings.nominatim_min_interval_s))
        now = time.monotonic()
        wait = interval - (now - _last_nominatim_mono) if _last_nominatim_mono else 0.0
        if wait > 0:
            await asyncio.sleep(wait)
            now = time.monotonic()
        _last_nominatim_mono = now
        return max(0.0, wait)


class GeocodeError(Exception):
    """Nominatim failed (network, non-200, or unusable payload)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class CityNotFoundError(Exception):
    """Nominatim returned zero matches for the query."""

    def __init__(self, city: str) -> None:
        super().__init__(city)
        self.city = city


def _headers() -> dict[str, str]:
    return {
        "User-Agent": settings.user_agent,
        "Referer": settings.nominatim_referer,
        "Accept": "application/json",
    }


async def geocode_city(city: str) -> dict:
    """Resolve ``city`` to ``{display_name, bbox, lat, lon, cache_hit}``.

    ``bbox`` is ``(south, west, north, east)`` floats. Takes the top
    Nominatim match — use :func:`search_places` when the caller needs to
    choose among several same-named places. Raises
    :class:`CityNotFoundError` on zero matches, :class:`GeocodeError` when
    Nominatim itself fails.
    """
    key = citymap_cache_key("geocode", city.strip().lower())
    cached = await cache_get(key)
    if cached is not None:
        payload = json.loads(cached)
        payload["cache_hit"] = True
        return payload

    # Cache miss → upstream: pace to the Nominatim 1 req/s policy first.
    await _throttle_nominatim()
    params = {"format": "jsonv2", "q": city, "limit": 1, "addressdetails": 0}
    try:
        async with httpx.AsyncClient(
            timeout=settings.nominatim_timeout_s, headers=_headers()
        ) as client:
            resp = await client.get(settings.nominatim_url, params=params)
    except httpx.HTTPError as exc:
        raise GeocodeError(str(exc)) from exc
    if resp.status_code != 200:
        raise GeocodeError(f"Nominatim answered HTTP {resp.status_code}")
    try:
        results = resp.json()
    except ValueError as exc:
        raise GeocodeError("Nominatim returned invalid JSON") from exc
    if not results:
        raise CityNotFoundError(city)

    top = results[0]
    try:
        south, north, west, east = (float(v) for v in top["boundingbox"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GeocodeError("Nominatim returned an unusable bounding box") from exc
    payload = {
        "display_name": top.get("display_name", city),
        "bbox": (south, west, north, east),
        "lat": float(top.get("lat", (south + north) / 2.0)),
        "lon": float(top.get("lon", (west + east) / 2.0)),
        "cache_hit": False,
    }
    await cache_set(key, json.dumps(payload))
    log.info("geocode.ok city=%r bbox=%s", city, payload["bbox"])
    return payload


def _candidate_from(top: dict, fallback_name: str) -> dict | None:
    """Build one candidate dict from a Nominatim result, or None when its
    bounding box is unusable."""
    try:
        south, north, west, east = (float(v) for v in top["boundingbox"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        lat = float(top.get("lat", (south + north) / 2.0))
        lon = float(top.get("lon", (west + east) / 2.0))
    except (TypeError, ValueError):
        return None
    return {
        "display_name": top.get("display_name", fallback_name),
        "bbox": (south, west, north, east),
        "lat": lat,
        "lon": lon,
        "category": top.get("class", ""),
        "type": top.get("type", ""),
    }


async def search_places(query: str, limit: int = 5) -> dict:
    """Search Nominatim for up to ``limit`` place candidates.

    Returns ``{candidates, cache_hit}`` where each candidate is
    ``{display_name, bbox, lat, lon, category, type}`` with ``bbox`` as
    ``(south, west, north, east)`` floats. Raises
    :class:`CityNotFoundError` on zero matches, :class:`GeocodeError` when
    Nominatim itself fails.
    """
    query = query.strip()
    limit = max(1, min(limit, 10))
    key = citymap_cache_key("geocode-search", query.lower(), str(limit))
    cached = await cache_get(key)
    if cached is not None:
        payload = json.loads(cached)
        payload["cache_hit"] = True
        return payload

    # Cache miss → upstream: pace to the Nominatim 1 req/s policy first.
    await _throttle_nominatim()
    params = {"format": "jsonv2", "q": query, "limit": limit, "addressdetails": 0}
    try:
        async with httpx.AsyncClient(
            timeout=settings.nominatim_timeout_s, headers=_headers()
        ) as client:
            resp = await client.get(settings.nominatim_url, params=params)
    except httpx.HTTPError as exc:
        raise GeocodeError(str(exc)) from exc
    if resp.status_code != 200:
        raise GeocodeError(f"Nominatim answered HTTP {resp.status_code}")
    try:
        results = resp.json()
    except ValueError as exc:
        raise GeocodeError("Nominatim returned invalid JSON") from exc
    if not results:
        raise CityNotFoundError(query)

    candidates = []
    for top in results[:limit]:
        candidate = _candidate_from(top, query)
        if candidate is not None:
            candidates.append(candidate)
    if not candidates:
        raise GeocodeError("Nominatim returned only unusable bounding boxes")
    payload = {"candidates": candidates, "cache_hit": False}
    await cache_set(key, json.dumps(payload))
    log.info("geocode.search query=%r candidates=%d", query, len(candidates))
    return payload


def check_bbox_span(bbox: BBox) -> None:
    """Raise :class:`BBoxTooLargeError` when the bbox exceeds the guard."""
    south, west, north, east = bbox
    if max(north - south, east - west) > settings.max_bbox_deg:
        raise BBoxTooLargeError(bbox)


class BBoxTooLargeError(Exception):
    """The requested area is too large for a single Overpass query."""

    def __init__(self, bbox: BBox) -> None:
        super().__init__(str(bbox))
        self.bbox = bbox
