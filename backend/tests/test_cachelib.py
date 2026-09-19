"""Shared cache core: compression, size accounting and batching.

The oversize rule is the one with teeth. ``cache_set`` returning False means
the render's ``svg_url`` 404s, and in production the two most expensive
Budapest fetches (38 MB and 40 MB of Overpass JSON) were tripping it and
being re-fetched from upstream on every single request.
"""

from __future__ import annotations

import asyncio
import json
import zlib

from backend.cachelib import RedisCache, decode_value, encode_value


class _Settings:
    def __init__(self, max_cached_bytes=32 * 1024 * 1024, compress_min_bytes=1024):
        self.cache_ttl_hours = 24
        self.max_cached_bytes = max_cached_bytes
        self.compress_min_bytes = compress_min_bytes


def _cache(**kw):
    settings = _Settings(**kw)
    return RedisCache("test:v1", lambda: settings, "test")


def test_small_values_are_stored_verbatim():
    raw = encode_value("hello", compress_min_bytes=1024)
    assert raw == b"hello"
    assert decode_value(raw) == "hello"


def test_large_values_round_trip_through_compression():
    text = json.dumps([{"type": "node", "id": i} for i in range(5000)])
    packed = encode_value(text, compress_min_bytes=1024)
    assert len(packed) < len(text.encode("utf-8")) / 4
    assert decode_value(packed) == text


def test_legacy_uncompressed_entries_still_read():
    """Entries written before compression existed have no magic header."""
    assert decode_value("plain".encode("utf-8")) == "plain"
    assert decode_value("plain") == "plain"


def test_compression_decides_the_oversize_verdict():
    """A payload over the cap raw but under it compressed is now stored.

    This is the production bug: dense OSM JSON deflates ~10x, so a 38 MB
    fetch fits comfortably in a 32 MB cap once stored compressed.
    """
    text = json.dumps([{"type": "way", "id": i, "nodes": [1, 2, 3]}
                       for i in range(200_000)])
    raw_len = len(text.encode("utf-8"))
    stored_len = len(encode_value(text, compress_min_bytes=1024))
    assert stored_len < raw_len / 4

    cache = _cache(max_cached_bytes=(raw_len + stored_len) // 2)
    assert asyncio.run(cache.set("k", text)) is True
    assert asyncio.run(cache.get("k")) == text


def test_oversize_even_compressed_is_skipped():
    cache = _cache(max_cached_bytes=32)
    big = "x" * 100_000  # compresses well, but not to under 32 bytes
    assert asyncio.run(cache.set("k", big)) is False
    assert asyncio.run(cache.get("k")) is None


def test_get_many_returns_only_hits():
    cache = _cache()
    asyncio.run(cache.set("a", "1"))
    asyncio.run(cache.set("c", "3"))
    got = asyncio.run(cache.get_many(["a", "b", "c"]))
    assert got == {"a": "1", "c": "3"}


def test_get_many_empty_is_a_noop():
    assert asyncio.run(_cache().get_many([])) == {}


def test_set_many_round_trips_and_reports_count():
    cache = _cache()
    stored = asyncio.run(cache.set_many({"a": "1", "b": "2"}))
    assert stored == 2
    assert asyncio.run(cache.get_many(["a", "b"])) == {"a": "1", "b": "2"}


def test_set_many_skips_only_the_oversize_entries():
    cache = _cache(max_cached_bytes=64)
    stored = asyncio.run(cache.set_many({"ok": "small", "big": "y" * 100_000}))
    assert stored == 1
    assert asyncio.run(cache.get("ok")) == "small"
    assert asyncio.run(cache.get("big")) is None


def test_expiry_is_honoured_in_the_memory_fallback():
    cache = _cache()
    asyncio.run(cache.set("k", "v", ttl_s=0))
    assert asyncio.run(cache.get("k")) is None


def test_keys_namespace_by_kind():
    cache = _cache()
    assert cache.key("svg", "a") != cache.key("tile", "a")
    assert cache.key("svg", "a") == cache.key("svg", "a")
    assert cache.key("svg", "a").startswith("test:v1:svg:")


class _FlakyRedis:
    """Every operation raises — a cache must degrade, never propagate."""

    async def get(self, key):
        raise RuntimeError("redis down")

    async def mget(self, keys):
        raise RuntimeError("redis down")

    async def setex(self, key, ttl, value):
        raise RuntimeError("redis down")

    def pipeline(self, transaction=False):
        raise RuntimeError("redis down")


def test_redis_failures_degrade_to_misses():
    cache = _cache()
    cache.configure(_FlakyRedis())
    assert asyncio.run(cache.get("k")) is None
    assert asyncio.run(cache.get_many(["k"])) == {}
    assert asyncio.run(cache.set("k", "v")) is False
    assert asyncio.run(cache.set_many({"k": "v"})) == 0


def test_corrupt_compressed_entry_reads_as_a_miss():
    """A truncated deflate stream must not take a request down."""

    class _CorruptRedis:
        async def get(self, key):
            return b"\x1fZL1" + zlib.compress(b"hello")[:4]

    cache = _cache()
    cache.configure(_CorruptRedis())
    assert asyncio.run(cache.get("k")) is None
