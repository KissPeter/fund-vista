"""Rate limiting for the CPU-exposed penplot endpoints (spec §5, review C.2.2).

The session server (conftest) runs with the limiter effectively disabled. The
``limited_server`` fixture spawns its own uvicorn subprocess with a tiny limit
so the 429 + envelope + Retry-After contract can be asserted over real HTTP.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import httpx
import pytest

from backend.penplot.ratelimit import RateLimiter, configure_redis
from backend.tests.conftest import REPO_ROOT, _free_port, _wait_for_health


@pytest.fixture(scope="session")
def limited_server(tmp_path_factory: pytest.TempPathFactory):
    """uvicorn subprocess with PENPLOT_RATE_LIMIT=2 (per-IP, 60s window)."""
    data_dir = str(tmp_path_factory.mktemp("penplot-rate-data"))
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env["PENPLOT_DATA_DIR"] = data_dir
    env["LOG_LEVEL"] = "WARNING"
    env["PENPLOT_RATE_LIMIT"] = "2"
    env["PENPLOT_RATE_LIMIT_WINDOW_S"] = "60"
    env["REDIS_CLOUD_URL"] = "redis://127.0.0.1:1/0"  # force memory fallback
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_health(base_url, proc)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _hammer(client: httpx.Client, n: int) -> list[httpx.Response]:
    # Route-level rate limit runs before endpoint validation; a well-formed
    # but unknown image_id yields 404 once past the limiter. 429 must appear
    # instead once the window is exhausted.
    return [
        client.post("/v1/convert", json={"image_id": "0" * 64, "params": {}})
        for _ in range(n)
    ]


def test_rate_limit_429_envelope_and_retry_after(limited_server):
    with httpx.Client(base_url=limited_server, timeout=30.0) as client:
        responses = _hammer(client, 5)
        statuses = [r.status_code for r in responses]
        assert statuses[:2] == [404, 404], statuses  # first two consume the window
        blocked = responses[2]
        assert blocked.status_code == 429
        body = blocked.json()
        assert body["error"]["code"] == "rate_limited"
        assert "Retry after" in body["error"]["message"]
        # HTTP-standard throttle headers are present.
        retry_after = int(blocked.headers["Retry-After"])
        assert 1 <= retry_after <= 60
        assert blocked.headers["X-Rate-Limit-Limit"] == "2"
        assert blocked.headers["X-Rate-Limit-Requests-Left"] == "0"
        # Later requests in the same window stay blocked.
        assert responses[3].status_code == 429 and responses[4].status_code == 429
        # Review D.1.3: the GETs share the same per-IP bucket — once the POST
        # window is spent, reads throttle too instead of serving unlimited.
        get = client.get("/v1/images/" + "0" * 64)
        assert get.status_code == 429


def test_rate_limit_whitelisted_ip_not_throttled(tmp_path_factory):
    # Limit of 1 would 429 on the second request — the whitelist must prevent
    # that, proving the per-IP exemption short-circuits the counter.
    data_dir = str(tmp_path_factory.mktemp("penplot-whitelist-data"))
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env["PENPLOT_DATA_DIR"] = data_dir
    env["LOG_LEVEL"] = "WARNING"
    env["PENPLOT_RATE_LIMIT"] = "1"
    env["PENPLOT_RATE_LIMIT_WHITELIST"] = "127.0.0.1"
    env["REDIS_CLOUD_URL"] = "redis://127.0.0.1:1/0"  # force memory fallback
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_health(base_url, proc)
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            responses = _hammer(client, 4)
            assert [r.status_code for r in responses] == [404, 404, 404, 404]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_rate_limiter_memory_fallback_blocks():
    # With no Redis the limiter still throttles (single-instance guarantee).
    configure_redis(None)
    limiter = RateLimiter(limit=2, window_s=60)

    async def _probe(ip: str):
        return await limiter.consume(ip)

    assert asyncio.run(_probe("1.2.3.4")) == (True, None)
    assert asyncio.run(_probe("1.2.3.4")) == (True, None)
    allowed, retry_after = asyncio.run(_probe("1.2.3.4"))
    assert allowed is False and 1 <= retry_after <= 60
    # Different IPs share the limit, not the bucket.
    assert asyncio.run(_probe("9.9.9.9")) == (True, None)


def test_rate_limiter_memory_prunes_stale_windows():
    # Review D.1.1: the in-process fallback must not grow forever — entries in
    # fully-elapsed windows are dropped on every consume.
    limiter = RateLimiter(limit=10, window_s=60)
    current = int(time.time() // 60)
    limiter._memory[("stale-a", current - 5)] = 99
    limiter._memory[("stale-b", current - 1)] = 3

    async def _probe(ip: str):
        return await limiter.consume(ip)

    assert asyncio.run(_probe("current")) == (True, None)
    assert set(limiter._memory) == {("current", current)}


class _FakeRedis:
    """Recorded async stub of the redis.asyncio surface the limiter touches."""

    def __init__(self, ttl_results: list[int] | None = None) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self._ttl_results = list(ttl_results or [])
        self._count = 0

    async def set(self, *args, **kwargs):
        self.calls.append(("set", args, kwargs))
        return True

    async def incr(self, *args, **kwargs):
        self.calls.append(("incr", args, kwargs))
        self._count += 1
        return self._count

    async def ttl(self, *args, **kwargs):
        self.calls.append(("ttl", args, kwargs))
        return self._ttl_results.pop(0) if self._ttl_results else 60

    async def expire(self, *args, **kwargs):
        self.calls.append(("expire", args, kwargs))


def test_rate_limiter_redis_path_atomic_set_nx_ex():
    # Review D.1.4: creation must stamp the TTL atomically (SET NX EX), not via
    # a separate EXPIRE that a crash can skip. Over-limit returns the TTL.
    fake = _FakeRedis()
    configure_redis(fake)  # type: ignore[arg-type]
    try:
        limiter = RateLimiter(limit=2, window_s=60)

        async def _probe(ip: str):
            return await limiter.consume(ip)

        assert asyncio.run(_probe("1.2.3.4")) == (True, None)
        assert asyncio.run(_probe("1.2.3.4")) == (True, None)
        allowed, retry_after = asyncio.run(_probe("1.2.3.4"))
        assert allowed is False and retry_after >= 1  # ttl result 59

        set_calls = [c for c in fake.calls if c[0] == "set"]
        assert set_calls, "SET must be used for atomic window creation"
        assert all(c[2].get("nx") is True and c[2].get("ex") == 60 for c in set_calls)
        # No stray expire when every key already carries a TTL.
        assert [c for c in fake.calls if c[0] == "expire"] == []
    finally:
        configure_redis(None)


def test_rate_limiter_redis_reasserts_expiry_when_ttl_missing():
    # INCR can race a just-expired key back into existence without a TTL; the
    # limiter must re-assert expiry instead of leaving a permanent key.
    fake = _FakeRedis(ttl_results=[60, -1])
    configure_redis(fake)  # type: ignore[arg-type]
    try:
        limiter = RateLimiter(limit=2, window_s=60)

        async def _probe(ip: str):
            return await limiter.consume(ip)

        assert asyncio.run(_probe("x")) == (True, None)  # ttl 60
        assert asyncio.run(_probe("x")) == (True, None)  # ttl -1 -> expire
        expires = [c for c in fake.calls if c[0] == "expire"]
        assert len(expires) == 1
        assert expires[0][2] == {"ex": 60} or expires[0][1][1] == 60
    finally:
        configure_redis(None)