"""Traffic Phase 1 API tests (TC-D43 live-boundary half, hermetic).

`GET /v1/traffic/availability` + `POST /v1/jobs type:traffic_ingest` over
**real HTTP** (in-process uvicorn on a socket — never TestClient, per the
test-design skill) with stubbed upstreams (no network):

* OurAirports → stubbed ``resolve_airport`` (HU / GB rows, not-found);
* GitHub release index → stubbed ``asource_status`` (or a MockTransport
  client that must never be hit);
* archive → stubbed ``_download_and_extract`` returning the fixture flights;
* durable store → ``FileSliceStore(tmp_path)``; Redis → memory fallback.

Same process ⇒ `monkeypatch` stubs apply to the serving app; the socket
round-trip still exercises the full stack (routing, middleware, envelopes).

Manual / `@live` follow-ups (cannot run hermetically): full-day
3.5 GB ingest wall-time + memory, `/data/traffic` growth, GitHub 5xx →
`traffic_source_unavailable` mapping, live OurAirports resolves
(LHBP/LHDC/LHSM), nginx host-only exclusion of `/v1/traffic/*`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import os
import socket
import tempfile
import threading
import time

os.environ.setdefault("PENPLOT_DATA_DIR", tempfile.mkdtemp(prefix="penplot-traffic-api-"))
os.environ["REDIS_CLOUD_URL"] = "redis://127.0.0.1:1/0"
os.environ["PENPLOT_RATE_LIMIT"] = "100000"
os.environ["TRAFFIC_STORE"] = "file"

import httpx
import pytest
import uvicorn

from backend.airports.ourairports import AirportNotFoundError
from backend.jobs import runner as jobs_runner
from backend.jobs import store as jobs_store

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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def live():
    """Real HTTP: in-process uvicorn on a socket (module scope, one server)."""
    from backend.main import app as full_app

    port = _free_port()
    # Pinned uvicorn (backend/pyproject.toml) has no `install_signal_handlers`
    # Config knob; its capture_signals() already no-ops in a non-main thread,
    # which is exactly how this fixture runs the server — so no kwarg needed.
    server = uvicorn.Server(uvicorn.Config(
        full_app, host="127.0.0.1", port=port,
        log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30.0
    with httpx.Client(timeout=2.0) as probe:
        while time.time() < deadline:
            try:
                if probe.get(f"{base}/healthz").status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("traffic test server never became healthy")
    with httpx.Client(base_url=base, timeout=60.0) as c:
        yield c
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    """Per-test state reset (the server is shared module-wide)."""
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
    yield
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


def _wait_done(c: httpx.Client, job_id: str, timeout_s: float = 30.0) -> dict:
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

def test_avail_ready_hit(live, monkeypatch, tmp_path):
    raw = _seed_l1(tmp_path / "traffic")
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    _stub_source_status(monkeypatch, {"status": "available"}, calls := [])
    resp = live.get("/v1/traffic/availability", params={"icao": "LHBP", "date": "2026-10-10"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ready", "bytes": len(raw)}
    assert calls == []  # L1 hit → no GitHub call


def test_avail_available(live, monkeypatch):
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    _stub_source_status(monkeypatch, {"status": "available", "tag": "v2026.10.10-planes-readsb-prod-0",
                                      "bytes": 3550429696, "parts": 2})
    resp = live.get("/v1/traffic/availability", params={"icao": "LHBP", "date": "2026-10-10"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "available"
    assert resp.json()["parts"] == 2


def test_avail_unavailable(live, monkeypatch):
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    _stub_source_status(monkeypatch, {"status": "unavailable", "reason": "no_release",
                                      "tag": "v2026.05.06-planes-readsb-prod-0"})
    resp = live.get("/v1/traffic/availability", params={"icao": "LHBP", "date": "2026-05-06"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "unavailable"


def test_avail_out_of_range_no_http(live, monkeypatch):
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    boom = httpx.MockTransport(lambda req: (_ for _ in ()).throw(AssertionError("github called")))
    orig = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: orig(transport=boom))
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    for day in (today, "2025-01-01"):
        resp = live.get("/v1/traffic/availability", params={"icao": "LHBP", "date": day})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "out_of_range", resp.json()
        assert "min" in resp.json() and "max" in resp.json()


def test_avail_unsupported_prefix_fastpath(live, monkeypatch):
    calls: list = []
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport", calls=calls)
    _stub_source_status(monkeypatch, {"status": "available"}, github_calls := [])
    for icao in ("EGLL", "KJFK"):
        resp = live.get("/v1/traffic/availability", params={"icao": icao, "date": "2026-10-10"})
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "unsupported_airport"
    assert calls == [] and github_calls == []  # no upstream I/O at all


def test_avail_unsupported_row_level(live, monkeypatch):
    calls: list = []
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport",
                  row={"ident": "LHGB", "iso_country": "GB"}, calls=calls)
    _stub_source_status(monkeypatch, {"status": "available"}, github_calls := [])
    resp = live.get("/v1/traffic/availability", params={"icao": "LHGB", "date": "2026-10-10"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "unsupported_airport"
    assert calls == ["LHGB"] and github_calls == []


def test_avail_invalid_and_not_found(live, monkeypatch):
    for params in ({"icao": "", "date": "2026-10-10"},
                   {"icao": "TOOLONG123", "date": "2026-10-10"},
                   {"icao": "LHBP", "date": "10/10/2026"}):
        resp = live.get("/v1/traffic/availability", params=params)
        assert resp.status_code == 422, (params, resp.text)
        assert resp.json()["error"]["code"] == "invalid_params"
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport",
                  exc=AirportNotFoundError("No airport found for ICAO 'LHZZ'."))
    resp = live.get("/v1/traffic/availability", params={"icao": "LHZZ", "date": "2026-10-10"})
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "airport_not_found"


def test_traffic_validation_never_rate_limits(live):
    for _ in range(3):
        r = live.get("/v1/traffic/availability", params={"icao": "", "date": "x"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_params", r.text
        r = live.post("/v1/jobs", json={"type": "traffic_ingest",
                                        "request": {"icao": "", "date": "x"}})
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "invalid_params"


# -- jobs --------------------------------------------------------------------

def test_jobs_traffic_enqueue_422(live):
    day = _yesterday_iso()
    for payload in ({"icao": "EGLL", "date": day},
                    {"icao": "LHBP", "date": "not-a-date"},
                    {"icao": "LHBP", "date": day, "extra": 1}):
        resp = live.post("/v1/jobs", json={"type": "traffic_ingest", "request": payload,
                                           "cancel_previous": False})
        assert resp.status_code == 422, (payload, resp.text)
        assert resp.json()["error"]["code"] == "invalid_params"
        assert "job_id" not in resp.json()
    assert live.get("/v1/jobs/" + "0" * 32).json()["error"]["code"] == "job_not_found"


def test_jobs_traffic_done_path(live, monkeypatch, tmp_path):
    _stub_resolve(monkeypatch, "backend.jobs._exec_traffic.resolve_airport")
    flights = _fixture_day()["flights"]
    dl_calls: list = []

    def _fake_download(day, specs, cancelled, on_progress=None):
        dl_calls.append(day.isoformat())
        assert [s["icao"] for s in specs] == ["LHBP"]
        return {"LHBP": flights}

    monkeypatch.setattr("backend.jobs._exec_traffic._download_and_extract", _fake_download)
    day = _yesterday_iso()
    resp = live.post("/v1/jobs", json={"type": "traffic_ingest",
                                       "request": {"icao": "LHBP", "date": day},
                                       "cancel_previous": False})
    assert resp.status_code == 202, resp.text
    assert resp.json()["poll_after_ms"] == 500
    final = _wait_done(live, resp.json()["job_id"])
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


def test_jobs_traffic_progress_counters(live, monkeypatch, tmp_path):
    """#66: the job doc must carry real counters, not just ``stage=download``.

    The download thread publishes ``{"stage", "done", "total"}`` events; the
    job poller copies them into the document, so a 4 GB pass is observable.
    """
    _stub_resolve(monkeypatch, "backend.jobs._exec_traffic.resolve_airport")
    flights = _fixture_day()["flights"]
    seen: list[dict] = []

    def _fake_download(day, specs, cancelled, on_progress=None):
        assert on_progress is not None, "the job must pass a progress callback"
        for done in range(1, 4):
            on_progress({"stage": "download", "done": done * 100, "total": 300})
            time.sleep(0.4)  # the poller ticks every second
            seen.append({"done": done * 100})
        on_progress({"stage": "parse", "done": 42, "total": 0})
        return {"LHBP": flights}

    monkeypatch.setattr("backend.jobs._exec_traffic._download_and_extract", _fake_download)
    day = _yesterday_iso()
    resp = live.post("/v1/jobs", json={"type": "traffic_ingest",
                                       "request": {"icao": "LHBP", "date": day},
                                       "cancel_previous": False})
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    # Poll while the job runs: a progress snapshot with counters must appear.
    deadline = time.time() + 15.0
    progress = None
    while time.time() < deadline:
        body = live.get(f"/v1/jobs/{job_id}").json()
        if body["status"] in ("done", "failed", "cancelled"):
            break
        if body.get("progress", {}).get("done"):
            progress = body["progress"]
            break
        time.sleep(0.2)
    final = _wait_done(live, job_id)
    assert final["status"] == "done", final
    assert progress is not None, "no progress counters were published"
    assert progress["stage"] in ("download", "parse")
    assert progress["done"] > 0
    if progress["stage"] == "download":
        assert progress["total"] == 300


def test_jobs_traffic_l1_hit_idempotent(live, monkeypatch, tmp_path):
    day = _yesterday_iso()
    raw = gzip.compress(json.dumps(_fixture_day(), separators=(",", ":")).encode("utf-8"))
    from backend.traffic.store import FileSliceStore, day_key

    FileSliceStore(tmp_path / "traffic").put(day_key(day, "LHBP"), raw)
    resolve_calls: list = []
    _stub_resolve(monkeypatch, "backend.jobs._exec_traffic.resolve_airport", calls=resolve_calls)
    monkeypatch.setattr("backend.jobs._exec_traffic._download_and_extract",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("download called")))
    resp = live.post("/v1/jobs", json={"type": "traffic_ingest",
                                       "request": {"icao": "LHBP", "date": day},
                                       "cancel_previous": False})
    assert resp.status_code == 202, resp.text
    final = _wait_done(live, resp.json()["job_id"])
    assert final["status"] == "done", final
    assert final["result"]["cache_hit"] is True
    assert final["result"]["movements"] == 4
    assert resolve_calls == []  # L1 recheck precedes lookup + download


def test_jobs_traffic_unsupported_row_fails(live, monkeypatch, tmp_path):
    _stub_resolve(monkeypatch, "backend.jobs._exec_traffic.resolve_airport",
                  row={"ident": "LHXX", "iso_country": "DE"})
    monkeypatch.setattr("backend.jobs._exec_traffic._download_and_extract",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("download called")))
    day = _yesterday_iso()
    resp = live.post("/v1/jobs", json={"type": "traffic_ingest",
                                       "request": {"icao": "LHXX", "date": day},
                                       "cancel_previous": False})
    assert resp.status_code == 202, resp.text
    final = _wait_done(live, resp.json()["job_id"])
    assert final["status"] == "failed", final
    assert final["error"]["code"] == "unsupported_airport"
    from backend.traffic.store import day_key

    assert not (tmp_path / "traffic" / day_key(day, "LHXX")).exists()


def test_jobs_traffic_out_of_range_fails(live, monkeypatch):
    resolve_calls: list = []
    _stub_resolve(monkeypatch, "backend.jobs._exec_traffic.resolve_airport", calls=resolve_calls)
    monkeypatch.setattr("backend.jobs._exec_traffic._download_and_extract",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("download called")))
    resp = live.post("/v1/jobs", json={"type": "traffic_ingest",
                                       "request": {"icao": "LHBP", "date": "2020-01-01"},
                                       "cancel_previous": False})
    assert resp.status_code == 202, resp.text
    final = _wait_done(live, resp.json()["job_id"])
    assert final["status"] == "failed", final
    assert final["error"]["code"] == "out_of_range"
    assert resolve_calls == []  # range check precedes lookup + download


def test_ingest_runtime_cap_exceeds_flat_cap():
    """#1031: the ingest override exists and loosens only the detached job,
    never the synchronously awaited render/convert cap."""
    from backend.jobs.schemas import MAX_RUNTIME_INGEST_S, MAX_RUNTIME_S

    assert MAX_RUNTIME_INGEST_S > MAX_RUNTIME_S


def test_jobs_traffic_timeout_uses_ingest_cap(live, monkeypatch):
    """#1031: the flat 300 s cap killed the ingest mid-download. The job is
    bound by ``MAX_RUNTIME_INGEST_S`` (patched small here); the timeout
    envelope carries the actual cap and the stop signal reaches the
    download thread, which returns on ``cancelled`` instead of streaming on
    detached."""
    import backend.jobs.runner as runner_mod

    stop_seen = threading.Event()

    def slow_download(day, specs, cancelled, on_progress=None):
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if cancelled():
                stop_seen.set()
                return {}
            time.sleep(0.05)
        return {}

    monkeypatch.setattr(runner_mod, "MAX_RUNTIME_INGEST_S", 0.5)
    monkeypatch.setattr("backend.jobs._exec_traffic._download_and_extract", slow_download)
    _stub_resolve(monkeypatch, "backend.jobs._exec_traffic.resolve_airport")
    day = _yesterday_iso()
    resp = live.post("/v1/jobs", json={"type": "traffic_ingest",
                                       "request": {"icao": "LHBP", "date": day},
                                       "cancel_previous": False})
    assert resp.status_code == 202, resp.text
    final = _wait_done(live, resp.json()["job_id"], timeout_s=15.0)
    assert final["status"] == "failed", final
    assert final["error"]["code"] == "timeout", final
    assert "exceeded" in final["error"]["message"] and "stopped." in final["error"]["message"], final
    assert stop_seen.wait(timeout=5.0), "download thread never saw the stop signal"


# -- multi-airport pass (one archive pass, many airports) --------------------

HU_ROW_DC = {
    "ident": "LHDC", "iso_country": "HU", "name": "Debrecen International",
    "latitude_deg": "47.4843", "longitude_deg": "21.6171", "elevation_ft": "354",
}


def _stub_resolve_multi(monkeypatch, rows: dict, calls: list | None = None):
    async def _fake(icao: str):
        if calls is not None:
            calls.append(icao)
        row = rows.get(icao)
        if row is None:
            raise AirportNotFoundError(icao)
        return (dict(row), False)

    monkeypatch.setattr("backend.jobs._exec_traffic.resolve_airport", _fake)


def test_jobs_traffic_multi_airport_done_path(live, monkeypatch, tmp_path):
    """One archive pass serves every airport of the list."""
    _stub_resolve_multi(monkeypatch, {"LHBP": HU_ROW, "LHDC": HU_ROW_DC})
    flights = _fixture_day()["flights"]
    dl_specs: list = []

    def _fake_download(day, specs, cancelled, on_progress=None):
        dl_specs.append([s["icao"] for s in specs])
        return {s["icao"]: flights for s in specs}

    monkeypatch.setattr("backend.jobs._exec_traffic._download_and_extract", _fake_download)
    day = _yesterday_iso()
    resp = live.post("/v1/jobs", json={"type": "traffic_ingest",
                                       "request": {"icaos": ["LHBP", "LHDC"], "date": day},
                                       "cancel_previous": False})
    assert resp.status_code == 202, resp.text
    final = _wait_done(live, resp.json()["job_id"])
    assert final["status"] == "done", final
    result = final["result"]
    assert dl_specs == [["LHBP", "LHDC"]]  # one pass for both airports
    assert result["date"] == day and result["errors"] == []
    by_icao = {e["icao"]: e for e in result["airports"]}
    assert set(by_icao) == {"LHBP", "LHDC"}
    for icao in ("LHBP", "LHDC"):
        assert by_icao[icao]["cache_hit"] is False
        assert by_icao[icao]["flights"] == len(flights)
        assert by_icao[icao]["bytes"] > 0
    from backend.traffic.store import day_key

    for icao in ("LHBP", "LHDC"):
        assert (tmp_path / "traffic" / day_key(day, icao)).is_file()


def test_jobs_traffic_multi_mixed_cache_hit(live, monkeypatch, tmp_path):
    """A cached airport is skipped; the pass covers only the rest."""
    _seed_l1(tmp_path / "traffic", _yesterday_iso(), "LHBP")
    _stub_resolve_multi(monkeypatch, {"LHBP": HU_ROW, "LHDC": HU_ROW_DC})
    dl_specs: list = []

    def _fake_download(day, specs, cancelled, on_progress=None):
        dl_specs.append([s["icao"] for s in specs])
        return {s["icao"]: [] for s in specs}

    monkeypatch.setattr("backend.jobs._exec_traffic._download_and_extract", _fake_download)
    day = _yesterday_iso()
    resp = live.post("/v1/jobs", json={"type": "traffic_ingest",
                                       "request": {"icaos": ["LHBP", "LHDC"], "date": day},
                                       "cancel_previous": False})
    assert resp.status_code == 202, resp.text
    final = _wait_done(live, resp.json()["job_id"])
    assert final["status"] == "done", final
    result = final["result"]
    assert dl_specs == [["LHDC"]]  # the cached airport costs no pass
    by_icao = {e["icao"]: e for e in result["airports"]}
    assert by_icao["LHBP"]["cache_hit"] is True
    assert by_icao["LHDC"]["cache_hit"] is False


def test_jobs_traffic_multi_partial_error(live, monkeypatch, tmp_path):
    """One bad airport is an error entry; the good one still lands."""
    _stub_resolve_multi(monkeypatch, {"LHBP": HU_ROW, "LHXX": {"ident": "LHXX",
                                                               "iso_country": "DE",
                                                               "name": "Elsewhere",
                                                               "latitude_deg": "50.0",
                                                               "longitude_deg": "8.0",
                                                               "elevation_ft": "300"}})
    flights = _fixture_day()["flights"]
    monkeypatch.setattr("backend.jobs._exec_traffic._download_and_extract",
                        lambda day, specs, cancelled, on_progress=None: {s["icao"]: flights for s in specs})
    day = _yesterday_iso()
    resp = live.post("/v1/jobs", json={"type": "traffic_ingest",
                                       "request": {"icaos": ["LHBP", "LHXX"], "date": day},
                                       "cancel_previous": False})
    assert resp.status_code == 202, resp.text
    final = _wait_done(live, resp.json()["job_id"])
    assert final["status"] == "done", final
    result = final["result"]
    assert [e["icao"] for e in result["airports"]] == ["LHBP"]
    assert [e["icao"] for e in result["errors"]] == ["LHXX"]
    assert result["errors"][0]["code"] == "unsupported_airport"
    assert result["errors"][0]["detail"]


def test_jobs_traffic_multi_shape_validation(live, monkeypatch):
    """Exactly one of icao / icaos; the list is capped."""
    monkeypatch.setattr("backend.jobs._exec_traffic._download_and_extract",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("download called")))
    day = _yesterday_iso()
    for request in ({"icao": "LHBP", "icaos": ["LHDC"], "date": day},
                    {"date": day},
                    {"icao": "", "date": day},
                    {"icaos": [], "date": day},
                    {"icaos": ["LHBP"] * 101, "date": day},
                    {"icaos": ["EGLL"], "date": day}):
        resp = live.post("/v1/jobs", json={"type": "traffic_ingest",
                                           "request": request, "cancel_previous": False})
        assert resp.status_code == 422, (request, resp.text)
        assert resp.json()["error"]["code"] == "invalid_params"
        assert "job_id" not in resp.json()


# -- render / import / results (Phase 2, TC-D45) ------------------------------

def _seed_l1_shifted(tmp_key_dir, day_iso: str) -> dict:
    """Seed L1 for ``day_iso`` with fixture flights shifted onto that day."""
    import datetime as _dt

    data = _fixture_day()
    base = _dt.datetime(2026, 10, 10, tzinfo=_dt.timezone.utc).timestamp()
    want = _dt.datetime.combine(_dt.date.fromisoformat(day_iso),
                                _dt.time(0), tzinfo=_dt.timezone.utc).timestamp()
    delta = want - base
    for fl in data["flights"]:
        fl["pts"] = [[p[0] + delta, p[1], p[2], p[3], p[4]] for p in fl["pts"]]
        fl["calls"] = [[c[0] + delta, c[1]] for c in fl["calls"]]
    data["date"] = day_iso
    from backend.traffic.store import FileSliceStore, day_key

    FileSliceStore(tmp_key_dir).put(
        day_key(day_iso, "LHBP"),
        gzip.compress(json.dumps(data, separators=(",", ":")).encode("utf-8")))
    return data


def _stub_basemap(monkeypatch, calls: list | None = None, failed: list | None = None):
    async def _fake(view, lat, lon):
        if calls is not None:
            calls.append(tuple(view))
        if failed:
            return ({k: [] for k in ("motorways", "waterways", "water", "airfield")}, list(failed))
        s, n, w, e = view
        line = [[[w, (s + n) / 2], [e, (s + n) / 2]]]
        return ({"motorways": line, "waterways": [], "water": [], "airfield": []}, [])

    monkeypatch.setattr("backend.traffic.router.load_basemap", _fake)


def _render_body(day: str, **kw) -> dict:
    body = {"icao": "LHBP", "date": day}
    body.update(kw)
    return body


def test_render_done_path(live, monkeypatch, tmp_path):
    day = _yesterday_iso()
    _seed_l1_shifted(tmp_path / "traffic", day)
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    base_calls: list = []
    _stub_basemap(monkeypatch, base_calls)
    resp = live.post("/v1/traffic/render", json=_render_body(day))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["icao"] == "LHBP" and body["date"] == day
    assert body["counts"] == {"arrivals": 1, "departures": 1, "holding": 1, "go_around": 0}
    assert body["movements"] == 4 and body["cache_hit"] is False
    assert "adsb.lol" in body["attribution"] and body["warnings"] == []
    assert "/v1/traffic/results/" in body["svg_url"] and len(body["svg_url"].rsplit("/", 1)[-1]) == 40
    assert base_calls != []
    # second identical render → L4 hit, no new basemap work
    resp2 = live.post("/v1/traffic/render", json=_render_body(day))
    assert resp2.json()["cache_hit"] is True
    assert resp2.json()["counts"] == body["counts"]
    assert len(base_calls) == 1
    # the svg_url serves chrome-free art
    from urllib.parse import urlsplit

    art = live.get(urlsplit(body["svg_url"]).path)
    assert art.status_code == 200, art.text
    assert art.headers["content-type"] == "image/svg+xml"
    assert 'id="traffic-arrivals"' in art.text and "<text" not in art.text


def test_render_filter_and_window_slicing(live, monkeypatch, tmp_path):
    day = _yesterday_iso()
    _seed_l1_shifted(tmp_path / "traffic", day)
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    _stub_basemap(monkeypatch)
    # only go-arounds on a day with none → empty but valid (TC-D42 backend half)
    resp = live.post("/v1/traffic/render", json=_render_body(
        day, arrivals=False, departures=False, holding=False, go_around=True))
    assert resp.status_code == 200, resp.text
    assert resp.json()["counts"] == {"arrivals": 0, "departures": 0, "holding": 0, "go_around": 0}
    assert resp.json()["movements"] == 4
    # window 06:00–07:00 keeps the 06:00 arrival; the 07:00 departure is out ([from, to))
    resp = live.post("/v1/traffic/render", json=_render_body(day, t_from="06:00", t_to="07:00"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["counts"]["arrivals"] == 1
    assert resp.json()["counts"]["departures"] == 0
    assert resp.json()["movements"] == 1


def test_render_view_rules(live, monkeypatch, tmp_path):
    day = _yesterday_iso()
    data = _seed_l1_shifted(tmp_path / "traffic", day)
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    _stub_basemap(monkeypatch)
    s, n, w, e = data["bbox"]
    view = {"view_south": s, "view_north": n, "view_west": w, "view_east": e}
    resp = live.post("/v1/traffic/render", json=_render_body(day, **view))
    assert resp.status_code == 200, resp.text  # view == ingest bbox is allowed
    resp = live.post("/v1/traffic/render", json=_render_body(
        day, view_south=s - 0.5, view_north=n, view_west=w, view_east=e))
    assert resp.status_code == 422, resp.text  # outside ingest bbox
    assert resp.json()["error"]["code"] == "invalid_params"
    resp = live.post("/v1/traffic/render", json=_render_body(day, view_south=s))
    assert resp.status_code == 422, resp.text  # partial view
    resp = live.post("/v1/traffic/render", json=_render_body(
        day, view_south=n, view_north=s, view_west=w, view_east=e))
    assert resp.status_code == 422, resp.text  # inverted view


def test_render_validation_boundaries(live, monkeypatch):
    day = _yesterday_iso()
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    bad = [
        _render_body("10/10/2026"),  # malformed date
        _render_body(day, t_from="06:00", t_to="06:00"),  # equal
        _render_body(day, t_from="18:00", t_to="06:00"),  # reversed
        _render_body(day, t_from="6:00"),  # malformed time
        _render_body(day, t_to="24:01"),  # past end of day
        _render_body(day, width=99),  # below min
        _render_body(day, width=4001),  # above max
        {"icao": "EGLL", "date": day},  # non-LH
        dict(_render_body(day), bogus=1),  # extra=forbid
    ]
    for payload in bad:
        resp = live.post("/v1/traffic/render", json=payload)
        assert resp.status_code == 422, (payload, resp.text)
        assert resp.json()["error"]["code"] in ("invalid_params", "unsupported_airport"), resp.json()
    ok = live.post("/v1/traffic/render", json=_render_body(day, width=100))
    assert ok.status_code in (200, 409), ok.text  # boundary accepted (409 = no L1 seeded)


def test_render_not_ingested_and_guards(live, monkeypatch):
    day = _yesterday_iso()
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    monkeypatch.setattr("backend.traffic.router.load_basemap",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("basemap called")))
    resp = live.post("/v1/traffic/render", json=_render_body(day))
    assert resp.status_code == 409, resp.text  # no L1 → no basemap work
    assert resp.json()["error"]["code"] == "traffic_not_ingested"
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    resp = live.post("/v1/traffic/render", json=_render_body(today))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "out_of_range"
    resp = live.post("/v1/traffic/render", json={"icao": "EGLL", "date": day})
    # Non-LH ICAOs are rejected at schema validation (invalid_params), same as
    # POST /v1/jobs traffic_ingest with EGLL (see test_jobs_traffic_enqueue_422
    # and the tolerant twin assertion in test_render_validation_boundaries).
    assert resp.status_code == 422 and resp.json()["error"]["code"] in ("invalid_params", "unsupported_airport")
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport",
                  exc=AirportNotFoundError("No airport found for ICAO 'LHZZ'."))
    resp = live.post("/v1/traffic/render", json={"icao": "LHZZ", "date": day})
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "airport_not_found"


def test_render_basemap_outage_degrades(live, monkeypatch, tmp_path):
    day = _yesterday_iso()
    _seed_l1_shifted(tmp_path / "traffic", day)
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    _stub_basemap(monkeypatch, failed=["motorways", "waterways", "water", "airfield"])
    resp = live.post("/v1/traffic/render", json=_render_body(day))
    assert resp.status_code == 200, resp.text  # never fails for a dead basemap
    assert "basemap_unavailable" in resp.json()["warnings"]
    assert resp.json()["counts"]["arrivals"] == 1  # tracks still draw


def test_import_registers_image(live, monkeypatch, tmp_path):
    day = _yesterday_iso()
    _seed_l1_shifted(tmp_path / "traffic", day)
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    _stub_basemap(monkeypatch)
    resp = live.post("/v1/traffic/import", json=_render_body(day))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["image_id"] and body["icao"] == "LHBP" and body["date"] == day
    assert body["counts"] == {"arrivals": 1, "departures": 1, "holding": 1, "go_around": 0}
    assert body["movements"] == 4 and "adsb.lol" in body["attribution"]


def test_results_tokens(live, monkeypatch, tmp_path):
    day = _yesterday_iso()
    _seed_l1_shifted(tmp_path / "traffic", day)
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    _stub_basemap(monkeypatch)
    token = live.post("/v1/traffic/render", json=_render_body(day)).json()["svg_url"].rsplit("/", 1)[-1]
    assert live.get(f"/v1/traffic/results/{token}").status_code == 200
    assert live.get("/v1/traffic/results/not-hex").status_code == 404
    assert live.get("/v1/traffic/results/" + "0" * 40).status_code == 404
