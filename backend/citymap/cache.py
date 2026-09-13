"""Redis cache for citymap data with an in-process fallback.

Same contract style as the main proxy cache (``fund-vista:*`` key space,
``setex`` TTL): geocode payloads, raw Overpass payloads and rendered SVGs
are stored as UTF-8 strings. When Redis is down or unconfigured the module
degrades to a small in-process dict with per-key expiry so a single
instance keeps working (and tests stay hermetic). Values bigger than
``max_cached_bytes`` are never stored — big-city payloads are served once
and re-fetched next time.
"""

from __future__ import annotations

import hashlib
import logging
import time

log = logging.getLogger(__name__)

KEY_PREFIX = "fund-vista:citymap:v1"

_redis = None  # async Redis client or None
_memory: dict[str, tuple[float, str]] = {}


def configure_citymap_redis(client) -> None:
    """Inject the shared Redis client (None = memory fallback)."""
    global _redis
    _redis = client
    _memory.clear()


def citymap_cache_key(kind: str, *parts: str) -> str:
    """Deterministic cache key: ``fund-vista:citymap:v1:{kind}:{sha1}``."""
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return f"{KEY_PREFIX}:{kind}:{digest}"


def _memory_prune(now: float) -> None:
    expired = [k for k, (exp, _) in _memory.items() if exp <= now]
    for k in expired:
        del _memory[k]


async def cache_get(key: str) -> str | None:
    """Return the cached UTF-8 string, or None on miss/expiry/error."""
    if _redis is not None:
        try:
            raw = await _redis.get(key)
        except Exception as exc:
            log.warning("citymap cache read failed for %s: %s", key, exc)
            return None
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else raw
    now = time.time()
    _memory_prune(now)
    entry = _memory.get(key)
    return entry[1] if entry is not None else None


async def cache_set(key: str, value: str, ttl_s: int | None = None) -> bool:
    """Store ``value`` for ``ttl_s`` seconds (default: ``cache_ttl_hours``).

    False when skipped (oversize) or the write failed.
    """
    from backend.citymap.config import settings

    if ttl_s is None:
        ttl_s = settings.cache_ttl_hours * 3600
    if len(value.encode("utf-8")) > settings.max_cached_bytes:
        log.info("citymap cache skip (oversize %d bytes): %s", len(value), key)
        return False
    if _redis is not None:
        try:
            await _redis.setex(key, ttl_s, value.encode("utf-8"))
            return True
        except Exception as exc:
            log.warning("citymap cache write failed for %s: %s", key, exc)
            return False
    _memory_prune(time.time())
    _memory[key] = (time.time() + ttl_s, value)
    return True
