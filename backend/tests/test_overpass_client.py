"""Shared Overpass client: 429 parking, Redis-shared park state, smart client."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from backend.http import tls, upstream
from backend.http.mirror_health import MirrorHealth
from backend.http.overpass_client import OverpassClient, OverpassConfig, OverpassError
from backend.logging_config import LOG_DATEFMT, LOG_FORMAT, configure_logging

URLS = ("https://a.test/api", "https://b.test/api", "https://c.test/api")


class _Resp:
    def __init__(self, status: int = 200, payload=None, headers=None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.headers = httpx.Headers(headers or {})
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Http:
    """Fake client: ``script`` maps mirror url -> list of responses."""

    def __init__(self, script):
        self.script = {u: list(r) for u, r in script.items()}
        self.calls: list[str] = []

    async def post(self, url, data=None, headers=None):
        self.calls.append(url)
        queue = self.script[url]
        return queue.pop(0) if len(queue) > 1 else queue[0]


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _client(http, mirrors, **cfg):
    config = OverpassConfig(urls=URLS, user_agent="t", retries=0, **cfg)
    return OverpassClient(
        config, semaphore=lambda: asyncio.Semaphore(2), mirrors=mirrors, http=http
    )


def _ok(n=1):
    return _Resp(200, {"elements": [{"type": "node", "id": n}]})


def test_429_parks_mirror_for_retry_after_and_fails_over_without_retry():
    async def go():
        clock = _Clock()
        mirrors = MirrorHealth("t", clock)
        http = _Http({
            URLS[0]: [_Resp(429, headers={"Retry-After": "90"})],
            URLS[1]: [_ok(7)],
            URLS[2]: [_ok(8)],
        })
        out = await _client(http, mirrors).fetch("q")
        ready, parked = await mirrors.split(URLS)
        return out, http.calls, ready, parked

    out, calls, ready, parked = asyncio.run(go())
    assert out == [{"type": "node", "id": 7}]
    assert calls == [URLS[0], URLS[1]]  # one 429, no same-mirror retry
    assert parked[0][0] == URLS[0] and 85 < parked[0][1] <= 90
    assert URLS[0] not in ready


def test_429_without_hint_uses_default_and_hint_is_clamped():
    async def go():
        clock = _Clock()
        mirrors = MirrorHealth("t", clock)
        http = _Http({
            URLS[0]: [_Resp(429)],
            URLS[1]: [_Resp(503, headers={"Retry-After": "99999"})],
            URLS[2]: [_ok()],
        })
        await _client(http, mirrors, default_park_s=40, max_park_s=120).fetch("q")
        _, parked = await mirrors.split(URLS)
        return dict(parked)

    parked = asyncio.run(go())
    assert round(parked[URLS[0]]) == 40
    assert round(parked[URLS[1]]) == 120


def test_parked_mirror_is_skipped_on_the_next_fetch():
    async def go():
        mirrors = MirrorHealth("t", _Clock())
        await mirrors.park(URLS[0], 60)
        http = _Http({u: [_ok()] for u in URLS})
        await _client(http, mirrors).fetch("q")
        return http.calls

    assert asyncio.run(go()) == [URLS[1]]


def test_all_parked_beyond_budget_raises_without_touching_the_network():
    async def go():
        mirrors = MirrorHealth("t", _Clock())
        for u in URLS:
            await mirrors.park(u, 300)
        http = _Http({u: [_ok()] for u in URLS})
        with pytest.raises(OverpassError) as ei:
            await _client(http, mirrors, budget_s=20).fetch("q")
        return ei.value, http.calls

    err, calls = asyncio.run(go())
    assert calls == []
    assert "parked" in err.detail


def test_all_parked_waits_for_the_soonest_when_it_fits_the_budget(monkeypatch):
    async def go():
        clock = _Clock()
        mirrors = MirrorHealth("t", clock)
        await mirrors.park(URLS[0], 5)
        await mirrors.park(URLS[1], 50)
        await mirrors.park(URLS[2], 50)
        slept: list[float] = []

        async def fake_sleep(s):
            slept.append(s)
            clock.now += s

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        http = _Http({u: [_ok(3)] for u in URLS})
        out = await _client(http, mirrors, budget_s=30).fetch("q")
        return out, slept, http.calls

    out, slept, calls = asyncio.run(go())
    assert out == [{"type": "node", "id": 3}]
    assert len(slept) == 1 and 5 <= slept[0] < 6
    assert calls == [URLS[0]]


def test_success_clears_the_park():
    async def go():
        clock = _Clock()
        mirrors = MirrorHealth("t", clock)
        await mirrors.park(URLS[0], 1)
        clock.now += 2  # park expired; a clean answer must also drop local state
        http = _Http({URLS[0]: [_ok()]})
        await _client(http, mirrors).fetch("q")
        return await mirrors.split(URLS)

    ready, parked = asyncio.run(go())
    assert ready[0] == URLS[0] and parked == []


class _FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def mget(self, keys):
        return [self.data.get(k) for k in keys]

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)


def test_park_is_shared_across_workers_through_redis():
    async def go():
        clock = _Clock()
        redis = _FakeRedis()
        worker1, worker2 = MirrorHealth("p", clock), MirrorHealth("p", clock)
        worker1.configure(redis)
        worker2.configure(redis)
        await worker1.park(URLS[0], 60)
        ready, parked = await worker2.split(URLS)
        await worker1.clear(URLS[0])
        ready_after, _ = await worker2.split(URLS)
        return ready, parked, ready_after

    ready, parked, ready_after = asyncio.run(go())
    assert URLS[0] not in ready and parked[0][0] == URLS[0]
    assert URLS[0] in ready_after


def test_redis_failure_degrades_to_local_memory():
    class _Broken:
        async def mget(self, keys):
            raise RuntimeError("down")

        async def set(self, *a, **k):
            raise RuntimeError("down")

    async def go():
        m = MirrorHealth("p", _Clock())
        m.configure(_Broken())
        await m.park(URLS[0], 30)
        return await m.split(URLS)

    ready, parked = asyncio.run(go())
    assert parked[0][0] == URLS[0]


def _patch_transport(monkeypatch, statuses, headers=None):
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        status = statuses[min(len(calls) - 1, len(statuses) - 1)]
        return httpx.Response(status, headers=headers or {}, json={"elements": []})

    monkeypatch.setattr(tls, "async_transport", lambda **k: httpx.MockTransport(handler))
    return calls


def test_smart_client_retries_transient_5xx_on_the_same_host(monkeypatch):
    calls = _patch_transport(monkeypatch, [502, 200])

    async def go():
        async with upstream.smart_client(
            "t", timeout=5, retries=1, backoff_s=0.0
        ) as client:
            return (await client.post("https://x.test/", data={"a": "b"})).status_code

    assert asyncio.run(go()) == 200
    assert len(calls) == 2


@pytest.mark.parametrize("status", [429, 503])
def test_smart_client_never_retries_429_or_503_in_place(monkeypatch, status):
    calls = _patch_transport(monkeypatch, [status], {"Retry-After": "7"})

    async def go():
        async with upstream.smart_client(
            "t", timeout=5, retries=3, backoff_s=0.0
        ) as client:
            return await client.post("https://x.test/")

    resp = asyncio.run(go())
    assert resp.status_code == status and len(calls) == 1
    assert upstream.retry_after_seconds(resp) == 7
    assert upstream.retry_after_seconds(httpx.Response(429), 12) == 12


def test_smart_client_logs_each_attempt_with_status(monkeypatch, caplog):
    _patch_transport(monkeypatch, [504, 200])
    caplog.set_level(logging.INFO, logger="fund-vista.http.upstream")

    async def go():
        async with upstream.smart_client("ovp", timeout=5, retries=1, backoff_s=0.0) as c:
            await c.post("https://x.test/")

    asyncio.run(go())
    lines = [r.getMessage() for r in caplog.records]
    assert any("name=ovp" in m and "status=504" in m for m in lines)
    assert any("name=ovp" in m and "status=200" in m for m in lines)


def test_log_format_carries_a_timestamp():
    assert "%(asctime)s" in LOG_FORMAT
    record = logging.LogRecord("x", logging.INFO, "f", 1, "hello", None, None)
    line = logging.Formatter(LOG_FORMAT, LOG_DATEFMT).format(record)
    assert line[:4].isdigit() and "INFO x: hello" in line
    configure_logging("INFO")  # idempotent, must not raise
