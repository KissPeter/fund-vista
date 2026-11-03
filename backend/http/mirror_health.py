"""Shared "parked mirror" memory for the Overpass fetchers.

When a public mirror answers ``429 Too Many Requests`` (or ``503`` with a
hint) it is telling us to stay away for a while. Retrying it — or letting
the other uvicorn workers keep hitting it — only deepens the block. A
parked mirror is therefore skipped until its deadline; the fetcher tries
the next one.

* The deadline is the server's ``Retry-After`` hint when it gave one,
  clamped to ``max_park_s``, else the caller's default.
* State lives in Redis (``upstream:park:<mirror hash>`` with a TTL) so all
  workers — and the citymap and airports fetchers, which share the same
  mirrors — see the same park. Without Redis (tests, outage) it degrades to
  per-process memory; Redis errors are swallowed to a warning.
* A mirror that answers usefully is cleared at once.

The module is settings-free (the ``backend.http`` convention).
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from typing import Callable, Iterable

log = logging.getLogger("fund-vista.http.mirror_health")


class MirrorHealth:
    """Parked mirrors, shared across workers through Redis when configured."""

    def __init__(
        self,
        prefix: str = "upstream:park",
        clock: Callable[[], float] = time.time,
    ) -> None:
        # Wall clock on purpose: deadlines are compared across processes.
        self._prefix = prefix
        self._clock = clock
        self._redis = None
        self._until: dict[str, float] = {}

    def configure(self, client) -> None:
        """Inject the shared async Redis client (None = memory only)."""
        self._redis = client

    def reset(self) -> None:
        """Forget every local park (tests; Redis keys expire on their own)."""
        self._until.clear()

    def _key(self, url: str) -> str:
        return f"{self._prefix}:{hashlib.sha1(url.encode()).hexdigest()[:16]}"

    async def _shared_deadlines(self, urls: list[str]) -> dict[str, float]:
        if self._redis is None or not urls:
            return {}
        try:
            raw = await self._redis.mget([self._key(u) for u in urls])
        except Exception as exc:  # Redis must never fail an upstream call
            log.warning("mirror_health.redis_read_failed error=%r", exc)
            return {}
        out: dict[str, float] = {}
        for url, value in zip(urls, raw):
            if value is None:
                continue
            try:
                out[url] = float(value)
            except (TypeError, ValueError):
                continue
        return out

    async def split(
        self, urls: Iterable[str]
    ) -> tuple[list[str], list[tuple[str, float]]]:
        """``(ready, parked)``: ready in configured order; parked as
        ``(url, seconds_left)`` ordered by the soonest recovery."""
        ordered = list(dict.fromkeys(urls))
        now = self._clock()
        shared = await self._shared_deadlines(ordered)
        ready: list[str] = []
        parked: list[tuple[str, float]] = []
        for url in ordered:
            until = max(self._until.get(url, 0.0), shared.get(url, 0.0))
            if until > now:
                parked.append((url, until - now))
            else:
                self._until.pop(url, None)
                ready.append(url)
        parked.sort(key=lambda item: item[1])
        return ready, parked

    async def park(self, url: str, seconds: float) -> None:
        """Skip ``url`` for ``seconds`` (all workers, when Redis is up)."""
        if seconds <= 0:
            return
        until = self._clock() + seconds
        self._until[url] = max(self._until.get(url, 0.0), until)
        if self._redis is None:
            return
        try:
            await self._redis.set(self._key(url), f"{until:.3f}", ex=math.ceil(seconds) + 1)
        except Exception as exc:
            log.warning("mirror_health.redis_write_failed error=%r", exc)

    async def clear(self, url: str) -> None:
        """A usable answer re-admits the mirror immediately."""
        if url in self._until:
            del self._until[url]
        if self._redis is None:
            return
        try:
            await self._redis.delete(self._key(url))
        except Exception as exc:
            log.warning("mirror_health.redis_write_failed error=%r", exc)


#: Process-wide registry shared by the citymap and airports Overpass fetchers
#: (same public mirrors, so one park serves both).
overpass_mirrors = MirrorHealth("upstream:park:overpass")


def configure_mirror_health_redis(client) -> None:
    """Wire the shared Redis client (called from the app lifespan)."""
    overpass_mirrors.configure(client)


__all__ = ["MirrorHealth", "configure_mirror_health_redis", "overpass_mirrors"]
