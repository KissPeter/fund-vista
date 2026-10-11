"""Traffic Phase 1 API tests (TC-D43 live-boundary half, hermetic).

`GET /v1/traffic/availability` + `POST /v1/jobs type:traffic_ingest`,
TestClient against the real app with stubbed upstreams (no network):

* OurAirports → stubbed ``resolve_airport`` (HU / GB rows, not-found);
* GitHub release index → stubbed ``asource_status`` (or a MockTransport
  client that must never be hit);
* archive → stubbed ``_download_and_extract`` returning the fixture flights;
* durable store → ``FileSliceStore(tmp_path)``; Redis → memory fallback.

A dedicated mock HTTP server is deliberately NOT used: every upstream
boundary is a plain function call, so monkeypatch is deterministic with
no ports or flakes (see plan: subagent API-test design, Phase 1).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import os
import tempfile
import time

os.environ.setdefault("PENPLOT_DATA_DIR", tempfile.mkdtemp(prefix="penplot-traffic-api-"))
os.environ["REDIS_CLOUD_URL"] = "redis://127.0.0.1:1/0"
os.environ["PENPLOT_RATE_LIMIT"] = "100000"
os.environ["TRAFFIC_STORE"] = "file"

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.airports.ourairports import AirportNotFoundError
from backend.jobs import runner as jobs_runner
from backend.jobs import store as jobs_store
from backend.main import app as full_app

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "traffic_day_lhbp.json")


def _fixture_day() -> dict:
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def _yesterday_iso() -> str:
    return (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()


HU_ROW = {
    "ident": "LHBP", "iso_country": "HU", "name": "Budapest Liszt Ferenc",
    "latitude_deg": "47.4369", "longitude_deg": "19.2556", "elevation_ft": "495",
}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    jobs_store.reset_memory()
    jobs_runner.reset_local()
    jobs_store.configure_jobs_redis(None)
    from backend.penplot import router as penplot_router

    penplot_router._limiter._memory.clear()
    from backend.traffic import cache as traffic_cache

    traffic_cache.configure_traffic_redis(None)
    from backend.traffic.store import FileSliceStore
    import backend.traffic.store as traffic_store

    monkeypatch.setattr(traffic_store, "default_store", lambda: FileSliceStore(tmp_path / "traffic"))
    with TestClient(full_app) as c:
        yield c
    jobs_runner.reset_local()
    jobs_store.reset_memory()


def _seed_l1(tmp_key_dir, day_iso: str = "2026-10-10", icao: str = "LHBP") -> bytes:
    from backend.traffic.store import FileSliceStore, day_key

    raw = gzip.compress(json.dumps(_fixture_day(), separators=(",", ":")).encode("utf-8"))
    store = FileSliceStore(tmp_key_dir)
    store.put(day_key(day_iso, icao), raw)
    return raw


def _stub_resolve(monkeypatch, where: str, row: dict | None = None, exc=None, calls: list | None = None):
    async def _fake(icao: str):
        if calls is not None:
            calls.append(icao)
        if exc is not None:
            raise exc
        return (dict(row or HU_ROW), False)

    monkeypatch.setattr(where, _fake)


def _stub_source_status(monkeypatch, body: dict, calls: list | None = None):
    async def _fake(day, client, today=None):
        if calls is not None:
            calls.append(day.isoformat())
        return dict(body)

    monkeypatch.setattr("backend.traffic.availability.asource_status", _fake)


def _wait_done(c: TestClient, job_id: str, timeout_s: float = 30.0) -> dict:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = c.get(f"/v1/jobs/{job_id}")
        assert last.status_code == 200, last.text
        if last.json()["status"] in ("done", "failed", "cancelled"):
            return last.json()
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} never finished: {last.json() if last else None}")


# -- availability ------------------------------------------------------------

def test_avail_ready_hit(client, monkeypatch, tmp_path):
    raw = _seed_l1(tmp_path / "traffic")
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    _stub_source_status(monkeypatch, {"status": "available"}, calls := [])
    resp = client.get("/v1/traffic/availability", params={"icao": "LHBP", "date": "2026-10-10"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ready", "bytes": len(raw)}
    assert calls == []  # L1 hit → no GitHub call


def test_avail_available(client, monkeypatch):
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    _stub_source_status(monkeypatch, {"status": "available", "tag": "v2026.10.10-planes-readsb-prod-0",
                                      "bytes": 3550429696, "parts": 2})
    resp = client.get("/v1/traffic/availability", params={"icao": "LHBP", "date": "2026-10-10"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "available"
    assert resp.json()["parts"] == 2


def test_avail_unavailable(client, monkeypatch):
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    _stub_source_status(monkeypatch, {"status": "unavailable", "reason": "no_release",
                                      "tag": "v2026.05.06-planes-readsb-prod-0"})
    resp = client.get("/v1/traffic/availability", params={"icao": "LHBP", "date": "2026-05-06"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "unavailable"


def test_avail_out_of_range_no_http(client, monkeypatch):
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    boom = httpx.MockTransport(lambda req: (_ for _ in ()).throw(AssertionError("github called")))
    orig = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: orig(transport=boom))
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    for day in (today, "2025-01-01"):
        resp = client.get("/v1/traffic/availability", params={"icao": "LHBP", "date": day})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "out_of_range", resp.json()
        assert "min" in resp.json() and "max" in resp.json()


def test_avail_unsupported_prefix_fastpath(client, monkeypatch):
    calls: list = []
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport", calls=calls)
    _stub_source_status(monkeypatch, {"status": "available"}, github_calls := [])
    for icao in ("EGLL", "KJFK"):
        resp = client.get("/v1/traffic/availability", params={"icao": icao, "date": "2026-10-10"})
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "unsupported_airport"
    assert calls == [] and github_calls == []  # no upstream I/O at all


def test_avail_unsupported_row_level(client, monkeypatch):
    calls: list = []
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport",
                  row={"ident": "LHGB", "iso_country": "GB"}, calls=calls)
    _stub_source_status(monkeypatch, {"status": "available"}, github_calls := [])
    resp = client.get("/v1/traffic/availability", params={"icao": "LHGB", "date": "2026-10-10"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "unsupported_airport"
    assert calls == ["LHGB"] and github_calls == []


def test_avail_invalid_and_not_found(client, monkeypatch):
    for params in ({"icao": "", "date": "2026-10-10"},
                   {"icao": "TOOLONG123", "date": "2026-10-10"},
                   {"icao": "LHBP", "date": "10/10/2026"}):
        resp = client.get("/v1/traffic/availability", params=params)
        assert resp.status_code == 422, (params, resp.text)
        assert resp.json()["error"]["code"] == "invalid_params"
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport",
                  exc=AirportNotFoundError("No airport found for ICAO 'LHZZ'."))
    resp = client.get("/v1/traffic/availability", params={"icao": "LHZZ", "date": "2026-10-10"})
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "airport_not_found"


def test_traffic_validation_never_rate_limits(client):
    for _ in range(3):
        r = client.get("/v1/traffic/availability", params={"icao": "", "date": "x"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_params", r.text
        r = client.post("/v1/jobs", json={"type": "traffic_ingest",
                                          "request": {"icao": "", "date": "x"}})
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "invalid_params"


# -- jobs --------------------------------------------------------------------

def test_jobs_traffic_enqueue_422(client):
    day = _yesterday_iso()
    for payload in ({"icao": "EGLL", "date": day},
                    {"icao": "LHBP", "date": "not-a-date"},
                    {"icao": "LHBP", "date": day, "extra": 1}):
        resp = client.post("/v1/jobs", json={"type": "traffic_ingest", "request": payload,
                                             "cancel_previous": False})
        assert resp.status_code == 422, (payload, resp.text)
        assert resp.json()["error"]["code"] == "invalid_params"
        assert "job_id" not in resp.json()
    assert client.get("/v1/jobs/" + "0" * 32).json()["error"]["code"] == "job_not_found"


def test_jobs_traffic_done_path(client, monkeypatch, tmp_path):
    _stub_resolve(monkeypatch, "backend.jobs._exec_traffic.resolve_airport")
    flights = _fixture_day()["flights"]
    dl_calls: list = []

    def _fake_download(day, specs, cancelled, on_progress=None):
        dl_calls.append(day.isoformat())
        assert [s["icao"] for s in specs] == ["LHBP"]
        return {"LHBP": flights}

    monkeypatch.setattr("backend.jobs._exec_traffic._download_and_extract", _fake_download)
    day = _yesterday_iso()
    resp = client.post("/v1/jobs", json={"type": "traffic_ingest",
                                         "request": {"icao": "LHBP", "date": day},
                                         "cancel_previous": False})
    assert resp.status_code == 202, resp.text
    assert resp.json()["poll_after_ms"] == 500
    final = _wait_done(client, resp.json()["job_id"])
    assert final["status"] == "done", final
    result = final["result"]
    assert result["icao"] == "LHBP" and result["date"] == day
    assert result["flights"] == len(flights) and result["movements"] == 4
    assert result["cache_hit"] is False and result["bytes"] > 0
    assert "attribution" in result and dl_calls == [day]
    from backend.traffic.store import day_key

    assert (tmp_path / "traffic" / day_key(day, "LHBP")).is_file()
    from backend.traffic import cache as traffic_cache
    from backend.traffic.schemas import movements_key

    l2 = asyncio.run(traffic_cache.cache_get(
        traffic_cache.traffic_cache_key("mov", movements_key("LHBP", day))))
    assert l2 is not None and len(json.loads(l2)) == 4


def test_jobs_traffic_l1_hit_idempotent(client, monkeypatch, tmp_path):
    day = _yesterday_iso()
    raw = gzip.compress(json.dumps(_fixture_day(), separators=(",", ":")).encode("utf-8"))
    from backend.traffic.store import FileSliceStore, day_key

    FileSliceStore(tmp_path / "traffic").put(day_key(day, "LHBP"), raw)
    resolve_calls: list = []
    _stub_resolve(monkeypatch, "backend.jobs._exec_traffic.resolve_airport", calls=resolve_calls)
    monkeypatch.setattr("backend.jobs._exec_traffic._download_and_extract",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("download called")))
    resp = client.post("/v1/jobs", json={"type": "traffic_ingest",
                                         "request": {"icao": "LHBP", "date": day},
                                         "cancel_previous": False})
    assert resp.status_code == 202, resp.text
    final = _wait_done(client, resp.json()["job_id"])
    assert final["status"] == "done", final
    assert final["result"]["cache_hit"] is True
    assert final["result"]["movements"] == 4
    assert resolve_calls == []  # L1 recheck precedes lookup + download


def test_jobs_traffic_unsupported_row_fails(client, monkeypatch, tmp_path):
    _stub_resolve(monkeypatch, "backend.jobs._exec_traffic.resolve_airport",
                  row={"ident": "LHXX", "iso_country": "DE"})
    monkeypatch.setattr("backend.jobs._exec_traffic._download_and_extract",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("download called")))
    day = _yesterday_iso()
    resp = client.post("/v1/jobs", json={"type": "traffic_ingest",
                                         "request": {"icao": "LHXX", "date": day},
                                         "cancel_previous": False})
    assert resp.status_code == 202, resp.text
    final = _wait_done(client, resp.json()["job_id"])
    assert final["status"] == "failed", final
    assert final["error"]["code"] == "unsupported_airport"
    from backend.traffic.store import day_key

    assert not (tmp_path / "traffic" / day_key(day, "LHXX")).exists()


def test_jobs_traffic_out_of_range_fails(client, monkeypatch):
    _stub_resolve(monkeypatch, "backend.jobs._exec_traffic.resolve_airport",
                  calls := [])
    monkeypatch.setattr("backend.jobs._exec_traffic._download_and_extract",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("download called")))
    resp = client.post("/v1/jobs", json={"type": "traffic_ingest",
                                         "request": {"icao": "LHBP", "date": "2020-01-01"},
                                         "cancel_previous": False})
    assert resp.status_code == 202, resp.text
    final = _wait_done(client, resp.json()["job_id"])
    assert final["status"] == "failed", final
    assert final["error"]["code"] == "out_of_range"
    assert calls == []  # range check precedes lookup + download
