"""Redis cache for airports data with an in-process fallback.

Thin binding over :mod:`backend.cachelib` (see :mod:`backend.citymap.cache`
for the sibling binding). Holds OurAirports CSVs, Overpass ``around``
payloads and rendered SVGs, transparently deflated above
``compress_min_bytes``.
"""

from __future__ import annotations

from backend.cachelib import RedisCache

KEY_PREFIX = "fund-vista:airports:v1"


def _settings():
    from backend.airports.config import settings

    return settings


_cache = RedisCache(KEY_PREFIX, _settings, "airports")


def configure_airports_redis(client) -> None:
    """Inject the shared Redis client (None = memory fallback)."""
    _cache.configure(client)


def airports_cache_key(kind: str, *parts: str) -> str:
    """Deterministic cache key: ``fund-vista:airports:v1:{kind}:{sha1}``."""
    return _cache.key(kind, *parts)


async def cache_get(key: str) -> str | None:
    """Return the cached UTF-8 string, or None on miss/expiry/error."""
    return await _cache.get(key)


async def cache_get_many(keys: list[str]) -> dict[str, str]:
    """MGET ``keys`` in one round-trip; misses are absent from the result."""
    return await _cache.get_many(keys)


async def cache_set(key: str, value: str, ttl_s: int | None = None) -> bool:
    """Store ``value`` for ``ttl_s`` seconds (default: ``cache_ttl_hours``).

    False when skipped (oversize) or the write failed.
    """
    return await _cache.set(key, value, ttl_s)


async def cache_set_many(items: dict[str, str], ttl_s: int | None = None) -> int:
    """Pipeline ``items`` in one round-trip. Returns the number stored."""
    return await _cache.set_many(items, ttl_s)
