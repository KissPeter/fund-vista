"""Redis cache for weather field subsets, with an in-process fallback.

Thin binding over :mod:`backend.cachelib`, the same pattern as
:mod:`backend.traffic.cache`. Values are strings, so the compressed npz bytes
travel base64-encoded (cachelib then deflates the text).
"""

from __future__ import annotations

from backend.cachelib import RedisCache

KEY_PREFIX = "fund-vista:weather:v1"


def _settings():
    from backend.weather.config import settings

    return settings


_cache = RedisCache(KEY_PREFIX, _settings, "weather")


def configure_weather_redis(client) -> None:
    """Inject the shared Redis client (None = memory fallback)."""
    _cache.configure(client)


def weather_cache_key(kind: str, *parts: str) -> str:
    """Deterministic cache key: ``fund-vista:weather:v1:{kind}:{sha1}``."""
    return _cache.key(kind, *parts)


async def cache_get(key: str) -> str | None:
    return await _cache.get(key)


async def cache_set(key: str, value: str, ttl_s: int | None = None) -> bool:
    return await _cache.set(key, value, ttl_s)
