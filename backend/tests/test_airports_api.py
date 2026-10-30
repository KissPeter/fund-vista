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

import json
import os
import socket
import tempfile
import threading
import time

os.environ.setdefault(
    "PENPLOT_DATA_DIR", tempfile.mkdtemp(prefix="penplot-airports-api-")
)
os.environ["PENPLOT_RATE_LIMIT"] = "100000"

import httpx
import pytest
import uvicorn

from backend.tests.helpers import default_params, svg_bytes, upload

from backend.tests import redis_harness


# ------------------------------------------------------------ server -------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def app_on_harness_redis():
    """The in-process app caches into the harness Redis for this module."""
    with redis_harness.app_on_harness_redis():
        yield


@pytest.fixture(scope="module")
def live(app_on_harness_redis):
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
    from backend.penplot import router as penplot_router

    upstream_mock.install()
    redis_harness.flush()  # cold cache per test; the server keeps its live client
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


def test_search_caches_ourairports_csv_in_redis(live: httpx.Client):
    assert live.get("/v1/airports/search?q=LHBP").status_code == 200
    cached = redis_harness.cached_values("fund-vista:airports:v1:ourairports:")
    assert cached, "OurAirports CSV was not written to Redis"
    # The mock fixture CSV is what got cached, not a network body.
    assert any("LHBP" in value for value in cached.values())


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


def test_render_stores_svg_and_overpass_payload_in_redis(live: httpx.Client):
    resp = live.post("/v1/airports/render", json={"icao": "LHBP"})
    assert resp.status_code == 200, resp.text
    svgs = redis_harness.cached_values("fund-vista:airports:v1:svg:")
    assert len(svgs) == 1
    (svg,) = svgs.values()
    assert svg.lstrip().startswith("<svg")
    # The Overpass "around" payload behind the render is cached too.
    overpass = redis_harness.cached_values("fund-vista:airports:v1:overpass")
    assert overpass, "Overpass payload was not written to Redis"
    assert any(json.loads(value) for value in overpass.values())  # non-empty JSON


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


def test_import_padding_zero_touches_margin_evenly(live: httpx.Client):
    """Convert of an imported airport: padding 0 → artwork reaches the margin
    rect on the limiting axis and the gaps on both axes are equal."""
    import re

    image_id = live.post("/v1/airports/import", json={"icao": "EGLL"}).json()["image_id"]
    params = {
        "method": "contour", "threshold": 128, "blur_radius": 1.0,
        "hatch_pitch_mm": 1.2, "contour_simplify": 2,
        "linemerge_tolerance_mm": 0.0, "linesimplify_tolerance_mm": 0.0,
        "linesort": True, "reloop_tolerance_mm": 0.0,
        "page": {"size": "A4", "orientation": "portrait", "margin_mm": 10,
                 "padding_mm": 0},
        "pen": {"pen_lift_s": 0.3},
    }
    resp = live.post("/v1/convert", json={"image_id": image_id, "params": params})
    assert resp.status_code == 200, resp.text
    svg = live.get(resp.json()["svg_url"]).text
    xs, ys = [], []
    for d in re.findall(r'<path d="([^"]+)"', svg):
        for x, y in re.findall(r"(-?\d+(?:\.\d+)?)[ ,]+(-?\d+(?:\.\d+)?)", d):
            xs.append(float(x))
            ys.append(float(y))
    gl, gr = min(xs) - 10.0, 200.0 - max(xs)
    gt, gb = min(ys) - 10.0, 287.0 - max(ys)
    assert abs(gl - gr) < 0.3 and abs(gt - gb) < 0.3
    assert min(gl, gt) < 0.3 and min(gr, gb) < 0.3  # limiting axis touches
    assert min(gl, gr, gt, gb) > -0.3  # no clipping past the margin


def test_import_returns_content_bbox_matching_cropped_image(live: httpx.Client):
    body = live.post("/v1/airports/import", json={"icao": "EGLL"}).json()
    bbox = body["content_bbox"]
    assert len(bbox) == 4 and bbox[2] > bbox[0] and bbox[3] > bbox[1]
    got = live.get(f"/v1/images/{body['image_id']}")
    assert got.status_code == 200, got.text


def test_convert_runway_hatch_band_keeps_full_strip_length(live: httpx.Client):
    """The 25 hatch lines of a runway strip must survive linemerge +
    curvesmooth at full length (Chaikin used to trim the merged U-turns, so
    the dense band came out ~half as long as the strip)."""
    import re

    lines = "".join(
        f'<path d="M {100 + i * 0.71:.2f} 100 L {100 + i * 0.71:.2f} 700"/>'
        for i in range(25)
    )
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="800" '
        'viewBox="0 0 400 800">'
        f'<g id="runways" fill="none" stroke="#000" stroke-width="7.69">{lines}</g>'
        '<path d="M 20 20 L 380 20 L 380 780 L 20 780 Z"/></svg>'
    )
    image_id = upload(live, svg.encode(), "rwy.svg").json()["image_id"]
    params = {
        "method": "contour", "threshold": 128, "blur_radius": 1.0,
        "hatch_pitch_mm": 1.2, "contour_simplify": 1.5, "curve_smooth": 1,
        "linemerge_tolerance_mm": 0.5, "linesimplify_tolerance_mm": 0.1,
        "linesort": True, "reloop_tolerance_mm": 0.05,
        "page": {"size": "A4", "orientation": "portrait", "margin_mm": 10,
                 "padding_mm": 0, "frame": False},
        "pen": {"pen_lift_s": 0.3},
    }
    resp = live.post("/v1/convert", json={"image_id": image_id, "params": params})
    assert resp.status_code == 200, resp.text
    final = live.get(resp.json()["svg_url"]).text
    segs = []  # lengths of the near-vertical hatch segments in the final
    for d in re.findall(r'<path d="([^"]+)"', final):
        pts = [(float(x), float(y)) for x, y in re.findall(r"(-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)", d)]
        if max(p[0] for p in pts) - min(p[0] for p in pts) > 10:
            continue  # the border, not the strip
        segs += [abs(b[1] - a[1]) for a, b in zip(pts, pts[1:]) if abs(b[1] - a[1]) > 5]
    assert len(segs) >= 20, "runway band missing from final"
    # the 800-unit canvas fits the 277 mm sheet height, so the 600-unit strip
    # is 207.75 mm; every hatch line must keep it all (Chaikin cut ~half).
    assert min(segs) > 0.99 * 600 * (277 / 800)


def test_convert_accepts_green_pen(live: httpx.Client):

    image_id = upload(live, svg_bytes(), "g.svg").json()["image_id"]
    params = default_params("contour")
    params["line_color"] = "green"
    resp = live.post("/v1/convert", json={"image_id": image_id, "params": params})
    assert resp.status_code == 200, resp.text
    assert 'stroke="#1E7A46"' in live.get(resp.json()["svg_url"]).text
