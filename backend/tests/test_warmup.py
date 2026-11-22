"""Cache warmer (backend/warmup.py): contract replay against a mocked shop and NAS."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import httpx

from backend import warmup
from backend.warmup import WarmupConfig, run_warmup, seconds_until_daily

SHOP = "https://shop.example"
TARGET = "https://nas.example"
DAY = "2026-11-19"

# Body strings deliberately carry non-canonical spacing and key order: the
# replayed bytes MUST match them exactly, not a re-serialised form.
BODY_A = '{"z": 1,  "a":[2, 3]}'
BODY_B = '{"width":210,"height":297}'

# A PCB-style multipart replay: fixed boundary, shipped sample file + params.
MULTIPART_CT = "multipart/form-data; boundary=----kp-warmup-pcb-v1"
MULTIPART_BODY = (
    "------kp-warmup-pcb-v1\r\n"
    'Content-Disposition: form-data; name="file"; filename="ESP32DIV2-pcb.GTL"\r\n'
    "Content-Type: application/octet-stream\r\n\r\n"
    "G04 sample board\r\n"
    "------kp-warmup-pcb-v1\r\n"
    'Content-Disposition: form-data; name="params"\r\n\r\n'
    '{"paper":"white","layers":{"pads":{"ink":"blue"}}}\r\n'
    "------kp-warmup-pcb-v1--\r\n"
)


def _item(name, path, body=BODY_A, content_type="application/json", **extra):
    return {"name": name, "method": "POST", "path": path, "body": body,
            "contentType": content_type, **extra}


def _ingest(name="traffic-ingest", icao="LHBP", date=DAY):
    return {"name": name, "kind": "traffic_ingest", "icao": icao, "date": date}


def _next(seq: list):
    """Pop the next scripted value; the last value repeats."""
    return seq.pop(0) if len(seq) > 1 else seq[0]


class Recorder:
    """Mock shop (contract) and mock NAS (replay, availability, jobs) in one transport."""

    def __init__(self, items, *, target_status=None, raise_for=(), contract_status=200,
                 verdicts=None, job_states=None):
        self.items = items
        self.target_status = target_status or {}
        self.raise_for = set(raise_for)
        self.contract_status = contract_status
        self.verdicts = list(verdicts or ["ready"])  # "error" answers HTTP 500
        self.job_states = list(job_states or ["done"])
        self.calls = []  # replayed POSTs: (path, body bytes, content-type)
        self.availability_checks = 0
        self.jobs_created = []  # JSON bodies of POST /v1/jobs
        self.job_deletes = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "shop.example":
            if self.contract_status != 200:
                return httpx.Response(self.contract_status)
            return httpx.Response(200, json={"generatedAt": "2026-11-20T00:00:00Z",
                                             "items": self.items})
        path = request.url.path
        if path == "/v1/traffic/availability":
            self.availability_checks += 1
            verdict = _next(self.verdicts)
            if verdict == "error":
                return httpx.Response(500)
            return httpx.Response(200, json={"status": verdict})
        if request.method == "POST" and path == "/v1/jobs":
            self.jobs_created.append(json.loads(request.content))
            return httpx.Response(202, json={"job_id": "j1", "status": "queued",
                                             "status_url": "/v1/jobs/j1",
                                             "cancel_url": "/v1/jobs/j1"})
        if request.method == "GET" and path == "/v1/jobs/j1":
            return httpx.Response(200, json={"job_id": "j1", "type": "traffic_ingest",
                                             "status": _next(self.job_states)})
        if request.method == "DELETE" and path.startswith("/v1/jobs/"):
            self.job_deletes.append(path)
            return httpx.Response(200, json={"job_id": "j1", "status": "cancelled"})

        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)  # yield, so a concurrent loop would overlap
            self.calls.append((path, request.content, request.headers.get("content-type")))
            if path in self.raise_for:
                raise httpx.ReadTimeout("slow target", request=request)
            return httpx.Response(self.target_status.get(path, 200), content=b"ok")
        finally:
            self.in_flight -= 1

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def _no_sleep(_seconds: float) -> None:
    return None


def _run(rec: Recorder, **kwargs) -> dict:
    kwargs.setdefault("poll_interval_s", 0)
    kwargs.setdefault("sleep", _no_sleep)

    async def go():
        async with rec.client() as client:
            return await run_warmup(SHOP, TARGET, client=client, **kwargs)

    return asyncio.run(go())


def test_replays_exact_body_bytes_against_target():
    rec = Recorder([
        _item("render", "/v1/render", BODY_A),
        _item("compose", "/v1/compose-sheet", BODY_B),
    ])
    summary = _run(rec)

    assert summary == {"total": 2, "ok": 2, "failed": 0, "skipped": 0}
    assert rec.calls == [
        ("/v1/render", BODY_A.encode("utf-8"), "application/json"),
        ("/v1/compose-sheet", BODY_B.encode("utf-8"), "application/json"),
    ]


def test_multipart_item_with_boundary_is_replayed_verbatim():
    """PCB warm-up: the exact multipart bytes and content type reach the target."""
    rec = Recorder([_item("pcb", "/v1/pcb/render", MULTIPART_BODY, MULTIPART_CT)])
    summary = _run(rec)

    assert summary == {"total": 1, "ok": 1, "failed": 0, "skipped": 0}
    assert rec.calls == [("/v1/pcb/render", MULTIPART_BODY.encode("utf-8"), MULTIPART_CT)]


def test_boundary_less_multipart_items_are_skipped():
    """A multipart body cannot be replayed without its delimiter."""
    rec = Recorder([
        _item("bare", "/v1/pcb/render", content_type="multipart"),
        _item("no-boundary", "/v1/pcb/render", content_type="multipart/form-data"),
        _item("empty-boundary", "/v1/pcb/render",
              content_type="multipart/form-data; boundary="),
        _item("whitespace-boundary", "/v1/pcb/render",
              content_type="multipart/form-data; boundary=   "),
        _item("pcb", "/v1/pcb/render", MULTIPART_BODY, MULTIPART_CT),
    ])
    summary = _run(rec)

    assert summary == {"total": 5, "ok": 1, "failed": 0, "skipped": 4}
    assert rec.calls == [("/v1/pcb/render", MULTIPART_BODY.encode("utf-8"), MULTIPART_CT)]


def test_content_type_supported_helper():
    assert warmup.content_type_supported("application/json")
    assert warmup.content_type_supported(MULTIPART_CT)
    assert warmup.content_type_supported("multipart/form-data; boundary=abc")
    for bad in ("multipart", "multipart/form-data", "multipart/form-data; boundary=",
                "multipart/form-data; boundary=   ", "text/plain", None, 123):
        assert not warmup.content_type_supported(bad)


def test_failing_items_do_not_stop_the_rest():
    rec = Recorder(
        [_item("bad", "/v1/render"), _item("slow", "/v1/convert"),
         _item("good", "/v1/compose-sheet")],
        target_status={"/v1/render": 500},
        raise_for={"/v1/convert"},
    )
    summary = _run(rec)

    assert summary == {"total": 3, "ok": 1, "failed": 2, "skipped": 0}
    assert [path for path, _, _ in rec.calls] == ["/v1/render", "/v1/convert", "/v1/compose-sheet"]


def test_items_run_sequentially():
    rec = Recorder([_item(f"i{n}", "/v1/render") for n in range(5)])
    _run(rec)
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
        _ingest(icao=""),
        _item("ok", "/v1/render"),
    ])
    summary = _run(rec)
    assert summary == {"total": 8, "ok": 1, "failed": 0, "skipped": 7}
    assert len(rec.calls) == 1


def test_contract_fetch_failure_replays_nothing_and_does_not_raise():
    rec = Recorder([_item("x", "/v1/render")], contract_status=503)
    summary = _run(rec)
    assert summary["total"] == 0
    assert rec.calls == []


def test_ready_day_skips_ingest_and_replays_traffic_render():
    rec = Recorder([
        _ingest(),
        _item("traffic-render", "/v1/traffic/render", BODY_B),
    ], verdicts=["ready"])
    summary = _run(rec)

    assert rec.jobs_created == []
    assert summary == {"total": 2, "ok": 1, "failed": 0, "skipped": 0}
    assert rec.calls == [("/v1/traffic/render", BODY_B.encode("utf-8"), "application/json")]


def test_available_day_runs_one_ingest_job_then_replays_render():
    rec = Recorder([
        _ingest(),
        _item("traffic-render", "/v1/traffic/render", BODY_B),
    ], verdicts=["available"], job_states=["queued", "running", "done"])
    summary = _run(rec)

    assert rec.jobs_created == [{"type": "traffic_ingest",
                                 "request": {"icao": "LHBP", "date": DAY}}]
    assert summary["ok"] == 1
    assert [path for path, _, _ in rec.calls] == ["/v1/traffic/render"]


def test_failed_ingest_job_keeps_traffic_render_out():
    rec = Recorder([
        _ingest(),
        _item("traffic-render", "/v1/traffic/render"),
        _item("render", "/v1/render"),
    ], verdicts=["available"], job_states=["running", "failed"])
    summary = _run(rec)

    assert len(rec.jobs_created) == 1
    assert [path for path, _, _ in rec.calls] == ["/v1/render"]
    assert summary["skipped"] == 1


def test_ingest_timeout_cancels_job_and_keeps_traffic_render_out():
    rec = Recorder([
        _ingest(),
        _item("traffic-render", "/v1/traffic/render"),
    ], verdicts=["available"], job_states=["running"])
    _run(rec, ingest_timeout_s=0)

    assert len(rec.jobs_created) == 1
    assert rec.job_deletes == ["/v1/jobs/j1"]
    assert rec.calls == []


def test_unpublished_day_is_retried_hourly_then_given_up():
    rec = Recorder([
        _ingest(),
        _item("traffic-render", "/v1/traffic/render"),
        _item("render", "/v1/render"),
    ], verdicts=["unavailable"])
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    summary = _run(rec, sleep=record_sleep)

    assert rec.availability_checks == 12
    assert sleeps == [3600.0] * 11  # 12 hourly checks in total
    assert rec.jobs_created == []
    assert [path for path, _, _ in rec.calls] == ["/v1/render"]
    assert summary["skipped"] == 1


def test_published_later_is_ingested_on_a_retry():
    rec = Recorder([
        _ingest(),
        _item("traffic-render", "/v1/traffic/render"),
    ], verdicts=["error", "unavailable", "available"], job_states=["done"])
    _run(rec)

    assert rec.availability_checks == 3
    assert len(rec.jobs_created) == 1
    assert [path for path, _, _ in rec.calls] == ["/v1/traffic/render"]


def test_traffic_render_without_ingest_item_is_skipped():
    rec = Recorder([_item("traffic-render", "/v1/traffic/render")])
    summary = _run(rec)
    assert summary == {"total": 1, "ok": 0, "failed": 0, "skipped": 1}
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

    asyncio.run(go())

    assert claims == ["warmup:startup:deploy-1"]
    assert len(rec.calls) == 1


def test_lost_claim_skips_the_run():
    rec = Recorder([_item("x", "/v1/render")])

    async def claim(key, ttl):
        return False

    async def go():
        await warmup._run_claimed(WarmupConfig(SHOP, TARGET, "v1"), claim, "warmup:startup:v1", 60)

    asyncio.run(go())
    assert rec.calls == []
