"""Render cache for pcb sheets with an in-process fallback.

Thin binding over :mod:`backend.cachelib`, like :mod:`backend.wordmark.cache`.
Holds rendered SVGs, the render params they were made with, and the request
index (file digest + params to design id).
"""

from __future__ import annotations

from backend.cachelib import RedisCache

KEY_PREFIX = "fund-vista:pcb:v1"


def _settings():
    from backend.pcb.config import settings

    return settings


_cache = RedisCache(KEY_PREFIX, _settings, "pcb")


def configure_pcb_redis(client) -> None:
    """Inject the shared Redis client (None = memory fallback)."""
    _cache.configure(client)


def pcb_cache_key(kind: str, *parts: str) -> str:
    """Deterministic cache key: ``fund-vista:pcb:v1:{kind}:{sha1}``."""
    return _cache.key(kind, *parts)


async def cache_get(key: str) -> str | None:
    """Return the cached UTF-8 string, or None on miss/expiry/error."""
    return await _cache.get(key)


async def cache_get_many(keys: list[str]) -> dict[str, str]:
    """MGET ``keys`` in one round-trip; misses are absent from the result."""
    return await _cache.get_many(keys)


async def cache_set_many(items: dict[str, str], ttl_s: int | None = None) -> int:
    """Pipeline ``items`` in one round-trip. Returns the number stored."""
    return await _cache.set_many(items, ttl_s)


def svg_cache_key(design_id: str) -> str:
    """Key of the stored sheet SVG for one design id (sha256 of the bytes)."""
    return pcb_cache_key("svg", design_id)


def meta_cache_key(design_id: str) -> str:
    """Key of the stored plot stats for one design id."""
    return pcb_cache_key("meta", design_id)


def params_cache_key(design_id: str) -> str:
    """Key of the canonical render params a design id was made with."""
    return pcb_cache_key("params", design_id)
