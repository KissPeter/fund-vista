"""Shared Redis cache core for the citymap and airports modules.

Both modules kept byte-identical copies of this logic (only the key prefix
and the log wording differed), which made it impossible to fix one without
forgetting the other. They now delegate here and keep only their prefix and
settings binding.

Two behaviours matter for cache hit rate:

*Compression* — raw Overpass payloads and rendered SVGs are JSON/XML text
that deflates 6-10x. Values above ``compress_min_bytes`` are stored zlib'd
behind a 4-byte magic header, and the ``max_cached_bytes`` ceiling is
checked against the *stored* size. Before this, dense metro fetches
(38-40 MB of JSON) blew the cap and were silently never cached, so the
most expensive renders in the system re-fetched from Overpass every time.
Values written before compression existed have no magic header and are
still read back verbatim, so the change needs no key-space bump.

*Batching* — tile-granular caching turns one lookup into dozens, so
``cache_get_many``/``cache_set_many`` use MGET and a pipeline. Doing that
serially would trade an Overpass round-trip for 90 Redis round-trips.
"""

from __future__ import annotations

import hashlib
import logging
import time
import zlib

# Marks a zlib-deflated value. Chosen to be invalid UTF-8 so it can never
# collide with a legacy plain-text entry written before compression.
_MAGIC = b"\x1fZL1"


def cache_key(prefix: str, kind: str, *parts: str) -> str:
    """Deterministic cache key: ``{prefix}:{kind}:{sha1}``."""
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{kind}:{digest}"


def encode_value(value: str, compress_min_bytes: int) -> bytes:
    """UTF-8 bytes, deflated behind ``_MAGIC`` when worth it."""
    raw = value.encode("utf-8")
    if len(raw) < compress_min_bytes:
        return raw
    return _MAGIC + zlib.compress(raw, 6)


def decode_value(raw: bytes | str) -> str:
    """Inverse of :func:`encode_value`, tolerating legacy plain entries."""
    if isinstance(raw, str):
        return raw
    if raw.startswith(_MAGIC):
        return zlib.decompress(raw[len(_MAGIC):]).decode("utf-8")
    return raw.decode("utf-8")


class RedisCache:
    """Redis-backed cache with an in-process fallback.

    When Redis is down or unconfigured this degrades to a dict with per-key
    expiry so a single instance keeps working and tests stay hermetic. All
    Redis errors are swallowed to a warning and reported as a miss.
    """

    def __init__(self, prefix: str, settings_loader, label: str) -> None:
        self.prefix = prefix
        self.label = label
        self._settings_loader = settings_loader
        self._log = logging.getLogger(f"backend.{label}.cache")
        self._redis = None
        self._memory: dict[str, tuple[float, str]] = {}

    # -- wiring -----------------------------------------------------------

    def configure(self, client) -> None:
        """Inject the shared Redis client (None = memory fallback)."""
        self._redis = client
        self._memory.clear()

    @property
    def redis(self):
        return self._redis

    def key(self, kind: str, *parts: str) -> str:
        return cache_key(self.prefix, kind, *parts)

    # -- internals --------------------------------------------------------

    def _prune(self, now: float) -> None:
        for k in [k for k, (exp, _) in self._memory.items() if exp <= now]:
            del self._memory[k]

    def _limits(self) -> tuple[int, int, int]:
        s = self._settings_loader()
        return (
            s.cache_ttl_hours * 3600,
            s.max_cached_bytes,
            getattr(s, "compress_min_bytes", 64 * 1024),
        )

    # -- reads ------------------------------------------------------------

    async def get(self, key: str) -> str | None:
        """Return the cached string, or None on miss/expiry/error."""
        if self._redis is not None:
            try:
                raw = await self._redis.get(key)
            except Exception as exc:
                self._log.warning("%s cache read failed for %s: %s", self.label, key, exc)
                return None
            if raw is None:
                return None
            try:
                return decode_value(raw)
            except Exception as exc:
                self._log.warning("%s cache decode failed for %s: %s", self.label, key, exc)
                return None
        now = time.time()
        self._prune(now)
        entry = self._memory.get(key)
        return entry[1] if entry is not None else None

    async def get_many(self, keys: list[str]) -> dict[str, str]:
        """MGET ``keys``; missing/undecodable entries are simply absent."""
        if not keys:
            return {}
        out: dict[str, str] = {}
        if self._redis is not None:
            try:
                values = await self._redis.mget(keys)
            except Exception as exc:
                self._log.warning("%s cache mget failed (%d keys): %s", self.label, len(keys), exc)
                return {}
            for key, raw in zip(keys, values):
                if raw is None:
                    continue
                try:
                    out[key] = decode_value(raw)
                except Exception as exc:
                    self._log.warning("%s cache decode failed for %s: %s", self.label, key, exc)
            return out
        now = time.time()
        self._prune(now)
        for key in keys:
            entry = self._memory.get(key)
            if entry is not None:
                out[key] = entry[1]
        return out

    # -- writes -----------------------------------------------------------

    async def set(self, key: str, value: str, ttl_s: int | None = None) -> bool:
        """Store ``value``. False when skipped (oversize) or the write failed."""
        default_ttl, max_bytes, compress_min = self._limits()
        if ttl_s is None:
            ttl_s = default_ttl
        payload = encode_value(value, compress_min)
        if len(payload) > max_bytes:
            self._log.info(
                "%s cache skip (oversize %d bytes stored, %d raw): %s",
                self.label, len(payload), len(value.encode("utf-8")), key,
            )
            return False
        if self._redis is not None:
            try:
                await self._redis.setex(key, ttl_s, payload)
                return True
            except Exception as exc:
                self._log.warning("%s cache write failed for %s: %s", self.label, key, exc)
                return False
        self._prune(time.time())
        self._memory[key] = (time.time() + ttl_s, value)
        return True

    async def set_many(self, items: dict[str, str], ttl_s: int | None = None) -> int:
        """Pipeline ``items`` in one round-trip. Returns the number stored."""
        if not items:
            return 0
        default_ttl, max_bytes, compress_min = self._limits()
        if ttl_s is None:
            ttl_s = default_ttl
        encoded: dict[str, bytes] = {}
        for key, value in items.items():
            payload = encode_value(value, compress_min)
            if len(payload) > max_bytes:
                self._log.info(
                    "%s cache skip (oversize %d bytes stored): %s",
                    self.label, len(payload), key,
                )
                continue
            encoded[key] = payload
        if not encoded:
            return 0
        if self._redis is not None:
            try:
                pipe = self._redis.pipeline(transaction=False)
                for key, payload in encoded.items():
                    pipe.setex(key, ttl_s, payload)
                await pipe.execute()
                return len(encoded)
            except Exception as exc:
                self._log.warning(
                    "%s cache pipeline write failed (%d keys): %s",
                    self.label, len(encoded), exc,
                )
                return 0
        self._prune(time.time())
        expiry = time.time() + ttl_s
        for key in encoded:
            self._memory[key] = (expiry, items[key])
        return len(encoded)
