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

Manual / `@live` follow-ups (cannot run hermetically): NAS full-day
3.5 GB ingest wall-time + memory, `/data/traffic` growth, GitHub 5xx →
`traffic_source_unavailable` mapping, live OurAirports resolves
(LHBP/LHDC/LHSM), nginx NAS-only exclusion of `/v1/traffic/*`.
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
    # uvicorn >= 0.30 no longer accepts install_signal_handlers; it already
    # no-ops on signal install in a non-main thread, so just drop the kwarg.
    server = uvicorn.Server(uvicorn.Config(
        full_app, host="127.0.0.1", port=port, log_level="warning",
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
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "unsupported_airport"
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


# TC-D50: Phase 5 filters over real HTTP ---------------------------------------
#
# A synthetic L1 day with fully determined movements (seeded on the day
# before yesterday so the shared server's L2 "mov" entry cannot collide
# with the fixture-based tests above):
#   A: A320 arrival, flow 13, low (min far AGL 2172 ft);
#   B: B738 departure, flow 31, climbing (min far AGL 2616 ft);
#   C: A333 arrival, flow 13, high plateau (min far AGL 7505 ft).
# Stats: movements 3, arrivals 2, departures 1, no flags, peak 06:00.

def _pos(hdg_from_airport: float, dkm: float):
    """Point ``dkm`` out from LHBP on the given heading (flat-earth, consistent
    with :func:`backend.traffic.geo.bearing`)."""
    import math as _m

    h = _m.radians(hdg_from_airport)
    return (47.4369 + (dkm * _m.cos(h)) / 111.2,
            19.2556 + (dkm * _m.sin(h)) / (111.2 * _m.cos(_m.radians(47.4369))))


def _synth_day(day_iso: str) -> dict:
    import datetime as _dt

    base = _dt.datetime.combine(_dt.date.fromisoformat(day_iso),
                                _dt.time(0), tzinfo=_dt.timezone.utc).timestamp()

    def _pts(radial, dists, alts, t0, step):
        pts = []
        for i, (d, alt) in enumerate(zip(dists, alts)):
            la, lo = _pos(radial, d)
            pts.append([t0 + i * step, round(la, 5), round(lo, 5), alt, 1 if alt == 0 else 0])
        return pts

    t_a, t_b, t_c = base + 6 * 3600, base + 6 * 3600 + 1800, base + 7 * 3600
    d10 = [30.0 * (1 - i / 9) for i in range(10)]
    flights = [
        dict(hex="aa", reg=None, type="A320", desc=None, operator=None, db_flags=0,
             calls=[[t_a, "WZZ1"]],
             pts=_pts(310.0, d10, [6000.0 - i * (6000.0 / 9) for i in range(10)], t_a, 60.0)),
        dict(hex="bb", reg=None, type="B738", desc=None, operator=None, db_flags=0,
             calls=[[t_b, "RYR2"]],
             pts=_pts(310.0, [30.0 * (i / 9) for i in range(10)],
                      [0.0] + [i * (7000.0 / 9) for i in range(1, 10)], t_b, 60.0)),
        dict(hex="cc", reg=None, type="A333", desc=None, operator=None, db_flags=0,
             calls=[[t_c, "DLH3"]],
             pts=_pts(310.0, [40.0 * (1 - i / 11) for i in range(12)],
                      [8000.0] * 8 + [4000.0, 1500.0, 500.0, 0.0], t_c, 30.0)),
    ]
    return {"date": day_iso, "bbox": [45.5369, 49.3369, 15.6556, 22.8556],
            "airport": {"lat": 47.4369, "lon": 19.2556, "elev_ft": 495.0},
            "flights": flights}


def _seed_l1_synth(tmp_key_dir, day_iso: str) -> dict:
    from backend.traffic.store import FileSliceStore, day_key

    data = _synth_day(day_iso)
    FileSliceStore(tmp_key_dir).put(
        day_key(day_iso, "LHBP"),
        gzip.compress(json.dumps(data, separators=(",", ":")).encode("utf-8")))
    return data


def _day_before_yesterday_iso() -> str:
    return (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=2)).isoformat()


def test_phase5_category_render_and_stats(live, monkeypatch, tmp_path):
    day = _day_before_yesterday_iso()
    _seed_l1_synth(tmp_path / "traffic", day)
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    _stub_basemap(monkeypatch)
    resp = live.post("/v1/traffic/render", json=_render_body(day, pen_mode="category"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["counts"] == {"wide": 1, "narrow": 2, "regional": 0, "other": 0,
                              "holding": 0, "go_around": 0}
    assert body["movements"] == 3 and body["cache_hit"] is False
    assert body["stats"] == {"movements": 3, "arrivals": 2, "departures": 1,
                             "go_arounds": 0, "go_around_rate": 0.0,
                             "holdings": 0, "holding_minutes": 0, "busiest_hour": "06:00"}
    from urllib.parse import urlsplit

    art = live.get(urlsplit(body["svg_url"]).path)
    assert art.status_code == 200, art.text
    assert 'id="traffic-narrow"' in art.text and 'id="traffic-wide"' in art.text
    # second identical render → L4 hit with the same stats
    again = live.post("/v1/traffic/render", json=_render_body(day, pen_mode="category"))
    assert again.json()["cache_hit"] is True
    assert again.json()["stats"] == body["stats"]


def test_phase5_filter_gates_over_http(live, monkeypatch, tmp_path):
    day = _day_before_yesterday_iso()
    _seed_l1_synth(tmp_path / "traffic", day)
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    _stub_basemap(monkeypatch)

    def _counts(**kw):
        resp = live.post("/v1/traffic/render", json=_render_body(day, **kw))
        assert resp.status_code == 200, (kw, resp.text)
        return resp.json()["counts"]

    assert _counts(flows=["13"]) == {"arrivals": 2, "departures": 0, "holding": 0, "go_around": 0}
    assert _counts(flows=["31"]) == {"arrivals": 0, "departures": 1, "holding": 0, "go_around": 0}
    assert _counts(type_categories=["wide"])["arrivals"] == 1  # only the A333
    assert _counts(alt_max_agl_ft=2500)["arrivals"] == 1  # A only (2172); B, C out
    assert _counts(alt_max_agl_ft=3000)["departures"] == 1  # A + B in, C out
    assert _counts(callsign="RYR2")["departures"] == 1
    both = _counts(flows=["13"], type_categories=["narrow"])
    assert both["arrivals"] == 1 and both["departures"] == 0  # gates intersect


def test_phase5_pen_mode_cache_separation(live, monkeypatch, tmp_path):
    day = _day_before_yesterday_iso()
    _seed_l1_synth(tmp_path / "traffic", day)
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    base_calls: list = []
    _stub_basemap(monkeypatch, base_calls)
    kind = live.post("/v1/traffic/render", json=_render_body(day))
    cat = live.post("/v1/traffic/render", json=_render_body(day, pen_mode="category"))
    assert kind.json()["cache_hit"] is False and cat.json()["cache_hit"] is False
    assert kind.json()["counts"] != cat.json()["counts"]  # different fingerprints, no aliasing
    assert live.post("/v1/traffic/render", json=_render_body(day)).json()["cache_hit"] is True
    assert live.post("/v1/traffic/render",
                     json=_render_body(day, pen_mode="category")).json()["cache_hit"] is True
    assert len(base_calls) == 2  # one basemap load per variant (L3 shared underneath)


def test_phase5_validation_boundaries(live, monkeypatch):
    day = _day_before_yesterday_iso()
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    bad = [
        _render_body(day, pen_mode="size"),
        _render_body(day, type_categories=["jumbo"]),
        _render_body(day, flows=["99"]),
        _render_body(day, flows=["7"]),
        _render_body(day, flows=["AB"]),
        _render_body(day, alt_max_agl_ft=0),
        _render_body(day, alt_max_agl_ft=-100),
        _render_body(day, alt_max_agl_ft=60001),
    ]
    for payload in bad:
        resp = live.post("/v1/traffic/render", json=payload)
        assert resp.status_code == 422, (payload, resp.text)
        assert resp.json()["error"]["code"] == "invalid_params", resp.json()


def test_phase5_import_carries_stats(live, monkeypatch, tmp_path):
    day = _day_before_yesterday_iso()
    _seed_l1_synth(tmp_path / "traffic", day)
    _stub_resolve(monkeypatch, "backend.traffic.router.resolve_airport")
    _stub_basemap(monkeypatch)
    resp = live.post("/v1/traffic/import", json=_render_body(day))
    assert resp.status_code == 200, resp.text
    assert resp.json()["stats"]["movements"] == 3
    assert resp.json()["counts"] == {"arrivals": 2, "departures": 1, "holding": 0, "go_around": 0}
