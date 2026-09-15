"""Per-IP fixed-window rate limiting for the CPU-exposed penplot endpoints.

Port of the affilio project's ``CheapLensThrottlingMiddleware`` scheme
(Redis ``INCR`` + ``EXPIRE`` fixed-window counter keyed by client IP, spec
§5 rate limit / review C.2.2). Two deliberate deviations from affilio:

* Redis is optional. When unavailable the limiter degrades to an in-process
  fixed-window counter, so a public endpoint always has a throttle even with
  no Redis — the filesystem store is already single-instance, so the process
  covers both states (the proxy cache and upload TTL already tolerate a dead
  Redis).
* The window key is an epoch bucket (``now // window_s``) instead of a
  clock-formatted string; same semantics, no parsing.
* All penplot endpoints (the two POSTs and both GETs) share one per-IP bucket,
  and ``X-Rate-Limit-*`` headers only appear on 429 — both documented
  v1 simplifications (review D.1.5), not tuning knobs.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)

_redis: Optional[Redis] = None
_degraded = False


def configure_redis(client: Optional[Redis]) -> None:
    """Point the limiter at the app's Redis client (or None to use memory)."""
    global _redis, _degraded
    _redis = client
    if client is not None:
        _degraded = False


def get_redis() -> Optional[Redis]:
    """Return the configured Redis client (or None when memory fallback)."""
    return _redis


class RateLimiter:
    """Fixed-window per-IP counter: Redis-backed, memory-degrading on failure."""

    def __init__(self, limit: int, window_s: int) -> None:
        self._limit = limit
        self._window_s = window_s
        self._memory: dict[tuple[str, int], int] = {}

    @property
    def limit(self) -> int:
        return self._limit

    def _redis_key(self, client_ip: str, window_idx: int) -> str:
        return f"fund-vista:penplot:ratelimit:{client_ip}:{window_idx}"

    async def consume(self, client_ip: str) -> tuple[bool, int | None]:
        """Count one request for ``client_ip``.

        Returns ``(allowed, retry_after_s)``; ``retry_after_s`` is ``None``
        when the request is allowed, else the seconds until the window resets.
        """
        global _degraded
        now = time.time()
        window_idx = int(now // self._window_s)
        redis = _redis
        if redis is not None and not _degraded:
            try:
                key = self._redis_key(client_ip, window_idx)
                # Atomic create (review D.1.4): SET NX EX stamps the TTL at
                # birth, so there is no crash window between INCR and EXPIRE
                # that leaks a permanent key. INCR can still race a key that
                # expired between SET and INCR back into existence with no
                # TTL, so re-assert expiry when ttl == -1.
                await redis.set(key, 1, nx=True, ex=self._window_s)
                count = await redis.incr(key)
                ttl = await redis.ttl(key)
                if ttl == -1:
                    await redis.expire(key, self._window_s)
                    ttl = self._window_s
                if count > self._limit:
                    return False, max(int(ttl), 1)
                return True, None
            except RedisError as exc:
                # Degrade for the rest of the process: repeated Redis failures
                # must not add latency to every request, and the memory window
                # is at least a bounded throttle.
                _degraded = True
                log.warning(
                    "Rate limiter degraded to in-process window (Redis error): %s", exc
                )
        # In-process fallback: one entry per (IP, active window). Stale
        # windows are pruned on every consume so the dict stays bounded by the
        # number of currently-active IPs (review D.1.1).
        if self._memory:
            self._memory = {
                key: count
                for key, count in self._memory.items()
                if key[1] >= window_idx
            }
        bucket = self._memory.get((client_ip, window_idx))
        if bucket is None:
            self._memory[(client_ip, window_idx)] = 1
            return True, None
        count = bucket + 1
        self._memory[(client_ip, window_idx)] = count
        if count > self._limit:
            retry_after = int(
                max(1.0, (window_idx + 1) * self._window_s - time.time())
            )
            return False, retry_after
        return True, None