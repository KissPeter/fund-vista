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
                count = await redis.incr(key)
                if count == 1:
                    await redis.expire(key, self._window_s)
                if count > self._limit:
                    ttl = await redis.ttl(key)
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