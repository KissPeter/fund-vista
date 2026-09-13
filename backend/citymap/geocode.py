"""Place-name resolution via Nominatim (the city-roads approach).

``geocode_city`` mirrors what city-roads' ``findBoundaryByName`` does for
the search box: ask Nominatim for the best match and take its bounding
box. Results are cached in Redis for ``cache_ttl_hours`` — Nominatim
policy allows at most 1 req/s, so every cache hit keeps us compliant.
"""

from __future__ import annotations

import json
import logging

import httpx

from backend.citymap.cache import cache_get, cache_set, citymap_cache_key
from backend.citymap.config import settings
from backend.citymap.overpass import BBox

log = logging.getLogger(__name__)


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
        "Referer": "https://github.com/anomalyco/opencode",
        "Accept": "application/json",
    }


async def geocode_city(city: str) -> dict:
    """Resolve ``city`` to ``{display_name, bbox, lat, lon, cache_hit}``.

    ``bbox`` is ``(south, west, north, east)`` floats. Raises
    :class:`CityNotFoundError` on zero matches, :class:`GeocodeError` when
    Nominatim itself fails.
    """
    key = citymap_cache_key("geocode", city.strip().lower())
    cached = await cache_get(key)
    if cached is not None:
        payload = json.loads(cached)
        payload["cache_hit"] = True
        return payload

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
