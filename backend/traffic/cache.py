"""Redis cache for traffic data with an in-process fallback.

Thin binding over :mod:`backend.cachelib` (same pattern as
:mod:`backend.airports.cache`). Holds L2 classified movements
(rebuildable from the durable L1 day dataset in the slice store).
"""

from __future__ import annotations

from backend.cachelib import RedisCache

KEY_PREFIX = "fund-vista:traffic:v1"


def _settings():
    from backend.traffic.config import settings

    return settings


_cache = RedisCache(KEY_PREFIX, _settings, "traffic")


def configure_traffic_redis(client) -> None:
    """Inject the shared Redis client (None = memory fallback)."""
    _cache.configure(client)


def traffic_cache_key(kind: str, *parts: str) -> str:
    """Deterministic cache key: ``fund-vista:traffic:v1:{kind}:{sha1}``."""
    return _cache.key(kind, *parts)


async def cache_get(key: str) -> str | None:
    """Return the cached UTF-8 string, or None on miss/expiry/error."""
    return await _cache.get(key)


async def cache_set(key: str, value: str, ttl_s: int | None = None) -> bool:
    """Store ``value`` for ``ttl_s`` seconds (default: ``cache_ttl_hours``)."""
    return await _cache.set(key, value, ttl_s)
