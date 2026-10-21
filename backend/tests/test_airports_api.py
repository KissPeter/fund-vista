"""Airports API happy paths + error classes over real HTTP (mock upstreams).

Live-server `not a TestClient` per the test-design skill — but unlike the
(subprocess-based) session `http_client` fixture, this file runs the app
IN-PROCESS (uvicorn daemon thread, per test_traffic_api.py) so
`monkeypatch` stubs apply to the serving app, and an autouse fixture
installs `backend.upstream_mock.install()` so every third-party call is
served from deterministic fixtures (no network, no real data; unmocked
hosts fail closed).

Coverage plan (per docs/api-integration-testing.md): every endpoint gets
happy-path + invalid/upstream-outage classes; mix of equivalence classes and
boundary analysis (limit 0/1/10/11, q length 1/2/60/61); one class per test.

Known gap covered here specifically: the full OurAirports CSV boundary
(fetch → parse → rank → envelope) which the unit suite never exercised end
to end — the 2026-10-20 pen-pixel 500 (unreadable certifi bundle raised on
httpx client construction) was only visible through it.
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time

os.environ.setdefault(
    "PENPLOT_DATA_DIR", tempfile.mkdtemp(prefix="penplot-airports-api-")
)
os.environ["REDIS_CLOUD_URL"] = "redis://127.0.0.1:1/0"
os.environ["PENPLOT_RATE_LIMIT"] = "100000"

import httpx
import pytest
import uvicorn


# ------------------------------------------------------------ server -------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def live():
    """Real HTTP: in-process uvicorn on a socket (module scope, one server)."""
    from backend.main import app as full_app

    port = _free_port()
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
            raise RuntimeError("airports API test server never became healthy")
    with httpx.Client(base_url=base, timeout=60.0) as client:
        yield client
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Mock edge + per-test state reset (the server is shared module-wide).

    mock install() wraps httpx client construction, so every outbound fetch
    the app makes after this point is served from fixtures; uninstall() at
    teardown restores stock clients for other modules.
    """
    from backend import upstream_mock
    from backend.airports import cache as airports_cache
    from backend.penplot import router as penplot_router

    upstream_mock.install()
    airports_cache.configure_airports_redis(None)  # also clears memory store
    penplot_router._limiter._memory.clear()
    yield
    upstream_mock.uninstall()


def _stub_fetch(monkeypatch, exc: Exception) -> None:
    """Any OurAirports CSV fetch fails with ``exc`` at the network boundary.

    Patches the uncapped seam (``_fetch_csv_uncached``), NOT ``_fetch_csv``:
    the wrapper gate (any unexpected failure at the boundary →
    OurAirportsError → 502 envelope) must be part of the exercised path,
    mirroring production.
    """

    async def _boom(name: str, client=None):
        raise exc

    import backend.airports.ourairports as mod

    monkeypatch.setattr(mod, "_fetch_csv_uncached", _boom)


# ------------------------------------------------------------- search ------

def test_search_exact_ident_lhbp(live: httpx.Client):
    resp = live.get("/v1/airports/search?q=LHBP")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query"] == "LHBP"
    assert body["candidates"][0]["icao"] == "LHBP"
    assert body["candidates"][0]["iata"] == "BUD"
    assert body["candidates"][0]["lat"] == pytest.approx(47.4369)


def test_search_exact_iata_resolves_lhbp(live: httpx.Client):
    resp = live.get("/v1/airports/search?q=BUD")
    assert resp.status_code == 200, resp.text
    assert resp.json()["candidates"][0]["icao"] == "LHBP"


def test_search_prefix_orders_large_first(live: httpx.Client):
    resp = live.get("/v1/airports/search?q=LH")
    assert resp.status_code == 200, resp.text
    icaos = [c["icao"] for c in resp.json()["candidates"]]
    # score-1 large airports first (LHBP ident-prefix, EGLL via IATA-prefix
    # "LHR"), then medium LHDC, then the smalls by name order.
    assert icaos[:3] == ["LHBP", "EGLL", "LHDC"]
    # Default limit 5: LHUD (name-sorted last of the smalls) is cut off.
    assert len(icaos) == 5
    assert "LHUD" not in icaos


def test_search_municipality_substring_matches_lhud(live: httpx.Client):
    resp = live.get("/v1/airports/search?q=Szeged")
    assert resp.status_code == 200, resp.text
    candidates = resp.json()["candidates"]
    assert [c["icao"] for c in candidates] == ["LHUD"]  # only small Szeged field


def test_search_no_match_is_200_empty(live: httpx.Client):
    resp = live.get("/v1/airports/search?q=zzzz")
    assert resp.status_code == 200, resp.text
    assert resp.json()["candidates"] == []


def test_search_cache_hit_on_second_call(live: httpx.Client):
    first = live.get("/v1/airports/search?q=EGLL").json()
    assert first["cache_hit"] is False
    second = live.get("/v1/airports/search?q=EGLL").json()
    assert second["cache_hit"] is True
    assert second["candidates"] == first["candidates"]


def test_search_limit_min_minus_1_is_422(live: httpx.Client):
    resp = live.get("/v1/airports/search?q=Budapest&limit=0")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


def test_search_limit_min_hits_exactly_one(live: httpx.Client):
    resp = live.get("/v1/airports/search?q=LH&limit=1")
    assert resp.status_code == 200
    assert len(resp.json()["candidates"]) == 1
    assert resp.json()["candidates"][0]["icao"] == "LHBP"


def test_search_limit_max_is_10_ok(live: httpx.Client):
    resp = live.get("/v1/airports/search?q=LH&limit=10")
    assert resp.status_code == 200
    assert len(resp.json()["candidates"]) == 6  # 5 LH* fixtures + EGLL (IATA prefix)


def test_search_limit_max_plus_1_is_422(live: httpx.Client):
    resp = live.get("/v1/airports/search?q=LH&limit=11")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


def test_search_q_too_short_is_422(live: httpx.Client):
    resp = live.get("/v1/airports/search?q=l")
    assert resp.status_code == 422


def test_search_q_min_2_chars_ok(live: httpx.Client):
    resp = live.get("/v1/airports/search?q=LH")
    assert resp.status_code == 200


def test_search_q_max_60_ok(live: httpx.Client):
    q = "a" * 60
    resp = live.get(f"/v1/airports/search?q={q}")
    assert resp.status_code == 200


def test_search_q_too_long_is_422(live: httpx.Client):
    q = "a" * 61
    resp = live.get(f"/v1/airports/search?q={q}")
    assert resp.status_code == 422


def test_search_upstream_outage_is_502_envelope(live, monkeypatch):
    _stub_fetch(monkeypatch, RuntimeError("csv upstream exploded"))
    resp = live.get("/v1/airports/search?q=LHBP")
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "ourairports_unavailable"


# ------------------------------------------------------------- lookup ------

def test_lookup_happy_path_lhbp(live: httpx.Client):
    resp = live.get("/v1/airports/lookup?icao=LHBP")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["icao"] == "LHBP"
    assert "Budapest" in body["name"]
    assert body["latitude_deg"] == pytest.approx(47.4369)
    assert len(body["runways"]) == 1
    assert body["runways"][0]["le_ident"] == "13L"
    assert body["frequencies"]
    assert body["cache_hit"] is False


def test_lookup_is_case_insensitive(live: httpx.Client):
    resp = live.get("/v1/airports/lookup?icao=lhbp")
    assert resp.status_code == 200, resp.text
    assert resp.json()["icao"] == "LHBP"


def test_lookup_accepts_iata(live: httpx.Client):
    resp = live.get("/v1/airports/lookup?icao=BUD")
    assert resp.status_code == 200, resp.text
    assert resp.json()["icao"] == "LHBP"


def test_lookup_unknown_icao_is_404_envelope(live: httpx.Client):
    resp = live.get("/v1/airports/lookup?icao=ZZZZ")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "airport_not_found"


def test_lookup_malformed_icao_is_422(live: httpx.Client):
    resp = live.get("/v1/airports/lookup?icao=ZZ!")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


def test_lookup_upstream_outage_is_502_envelope(live, monkeypatch):
    """Network-level outage representative (httpx class) — same fail-closed
    gate as the RuntimeError representative in the search tests."""
    _stub_fetch(monkeypatch, httpx.ConnectError("network down"))
    resp = live.get("/v1/airports/lookup?icao=LHBP")
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "ourairports_unavailable"


# ------------------------------------------------------------- render ------

def test_render_happy_path_serves_svg(live: httpx.Client):
    resp = live.post("/v1/airports/render", json={"icao": "LHBP"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["icao"] == "LHBP"
    assert body["cache_hit"] is False
    assert body["path_counts"]  # some drawable geometry came out
    svg = live.get(body["svg_url"])
    assert svg.status_code == 200, svg.text[:200]
    assert "image/svg+xml" in svg.headers["content-type"]
    assert "<svg" in svg.text


def test_render_second_call_hits_cache(live: httpx.Client):
    body = {"icao": "LHPP", "radius_m": 500.0}
    first = live.post("/v1/airports/render", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["cache_hit"] is False
    second = live.post("/v1/airports/render", json=body)
    assert second.status_code == 200
    assert second.json()["cache_hit"] is True


def test_render_upstream_outage_is_502_envelope(live, monkeypatch):
    _stub_fetch(monkeypatch, RuntimeError("csv upstream exploded"))
    resp = live.post("/v1/airports/render", json={"icao": "LHBP"})
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "ourairports_unavailable"


# ------------------------------------------------------------- import ------

def test_import_registers_penplot_image(live: httpx.Client):
    resp = live.post("/v1/airports/import", json={"icao": "EGLL"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    image_id = body["image_id"]
    assert len(image_id) == 64 and int(image_id, 16) >= 0  # sha256 hex
    # registered as a penplot image: convert flow can pick it up
    got = live.get(f"/v1/images/{image_id}")
    assert got.status_code == 200, got.text
    assert got.json()["image_id"] == image_id
    assert got.json()["is_vector"] is True


def test_import_upstream_outage_is_502_envelope(live, monkeypatch):
    _stub_fetch(monkeypatch, RuntimeError("csv upstream exploded"))
    resp = live.post("/v1/airports/import", json={"icao": "EGLL"})
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "ourairports_unavailable"
