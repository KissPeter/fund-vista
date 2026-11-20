"""Cache warmer (backend/warmup.py): contract replay against a mocked shop and target."""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx

from backend import warmup
from backend.warmup import WarmupConfig, run_warmup, seconds_until_daily

SHOP = "https://shop.example"
TARGET = "https://nas.example"

# Body strings deliberately carry non-canonical spacing and key order: the
# replayed bytes MUST match them exactly, not a re-serialised form.
BODY_A = '{"z": 1,  "a":[2, 3]}'
BODY_B = '{"width":210,"height":297}'


def _item(name, path, body=BODY_A, content_type="application/json", **extra):
    return {"name": name, "method": "POST", "path": path, "body": body,
            "contentType": content_type, **extra}


class Recorder:
    """Mock shop (contract endpoint) and mock NAS (replay targets) in one transport."""

    def __init__(self, items, *, target_status=None, raise_for=(), contract_status=200):
        self.items = items
        self.target_status = target_status or {}
        self.raise_for = set(raise_for)
        self.contract_status = contract_status
        self.calls = []  # (path, body bytes, content-type)
        self.in_flight = 0
        self.max_in_flight = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "shop.example":
            if self.contract_status != 200:
                return httpx.Response(self.contract_status)
            return httpx.Response(200, json={"generatedAt": "2026-11-20T00:00:00Z",
                                             "items": self.items})
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)  # yield, so a concurrent loop would overlap
            self.calls.append((request.url.path, request.content,
                               request.headers.get("content-type")))
            if request.url.path in self.raise_for:
                raise httpx.ReadTimeout("slow target", request=request)
            return httpx.Response(self.target_status.get(request.url.path, 200), content=b"ok")
        finally:
            self.in_flight -= 1

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _run(coro):
    return asyncio.run(coro)


def test_replays_exact_body_bytes_against_target():
    rec = Recorder([
        _item("render", "/v1/render", BODY_A),
        _item("compose", "/v1/compose-sheet", BODY_B),
    ])

    async def go():
        async with rec.client() as client:
            return await run_warmup(SHOP, TARGET, client=client)

    summary = _run(go())

    assert summary == {"total": 2, "ok": 2, "failed": 0, "skipped": 0}
    assert rec.calls == [
        ("/v1/render", BODY_A.encode("utf-8"), "application/json"),
        ("/v1/compose-sheet", BODY_B.encode("utf-8"), "application/json"),
    ]


def test_failing_items_do_not_stop_the_rest():
    rec = Recorder(
        [_item("bad", "/v1/render"), _item("slow", "/v1/convert"),
         _item("good", "/v1/compose-sheet")],
        target_status={"/v1/render": 500},
        raise_for={"/v1/convert"},
    )

    async def go():
        async with rec.client() as client:
            return await run_warmup(SHOP, TARGET, client=client)

    summary = _run(go())

    assert summary == {"total": 3, "ok": 1, "failed": 2, "skipped": 0}
    assert [path for path, _, _ in rec.calls] == ["/v1/render", "/v1/convert", "/v1/compose-sheet"]


def test_items_run_sequentially():
    rec = Recorder([_item(f"i{n}", "/v1/render") for n in range(5)])

    async def go():
        async with rec.client() as client:
            await run_warmup(SHOP, TARGET, client=client)

    _run(go())
    assert len(rec.calls) == 5
    assert rec.max_in_flight == 1


def test_invalid_and_unsupported_items_are_skipped():
    rec = Recorder([
        _item("abs", "https://evil.example/v1/render"),
        _item("proto", "//evil.example/v1/render"),
        {"name": "get", "method": "GET", "path": "/v1/render", "body": "",
         "contentType": "application/json"},
        _item("multi", "/v1/compose-sheet", content_type="multipart"),
        _item("nonstr", "/v1/render", body={"a": 1}),
        "not-an-object",
        _item("ok", "/v1/render"),
    ])

    async def go():
        async with rec.client() as client:
            return await run_warmup(SHOP, TARGET, client=client)

    summary = _run(go())
    assert summary == {"total": 7, "ok": 1, "failed": 0, "skipped": 6}
    assert len(rec.calls) == 1


def test_contract_fetch_failure_replays_nothing_and_does_not_raise():
    rec = Recorder([_item("x", "/v1/render")], contract_status=503)

    async def go():
        async with rec.client() as client:
            return await run_warmup(SHOP, TARGET, client=client)

    summary = _run(go())
    assert summary["total"] == 0
    assert rec.calls == []


def test_seconds_until_daily_is_next_0005_local():
    assert seconds_until_daily(datetime(2026, 11, 20, 0, 0, 0)) == 5 * 60
    assert seconds_until_daily(datetime(2026, 11, 20, 0, 5, 0)) == 24 * 3600
    assert seconds_until_daily(datetime(2026, 11, 20, 13, 0, 0)) == 11 * 3600 + 5 * 60


def test_disabled_unless_both_settings_present():
    assert not WarmupConfig("", "", "v1").enabled
    assert not WarmupConfig(SHOP, "", "v1").enabled
    assert not WarmupConfig("", TARGET, "v1").enabled
    assert WarmupConfig(SHOP, TARGET, "v1").enabled


def test_start_creates_no_tasks_when_disabled():
    # A disabled config returns before touching the loop, so no running loop is needed.
    assert warmup.start(WarmupConfig("", "", "v1")) == []


def test_startup_trigger_runs_once_when_claimed(monkeypatch):
    rec = Recorder([_item("x", "/v1/render")])
    claims = []

    async def claim(key, ttl):
        claims.append(key)
        return True

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(rec.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(warmup.httpx, "AsyncClient", fake_client)

    async def go():
        tasks = warmup.start(WarmupConfig(SHOP, TARGET, "deploy-1"), claim=claim)
        assert len(tasks) == 2
        for _ in range(100):
            if rec.calls:
                break
            await asyncio.sleep(0.01)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    _run(go())

    assert claims == ["warmup:startup:deploy-1"]
    assert len(rec.calls) == 1


def test_lost_claim_skips_the_run():
    rec = Recorder([_item("x", "/v1/render")])

    async def claim(key, ttl):
        return False

    async def go():
        await warmup._run_claimed(WarmupConfig(SHOP, TARGET, "v1"), claim, "warmup:startup:v1", 60)

    _run(go())
    assert rec.calls == []
