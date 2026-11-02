"""Citymap API tests (live-boundary half, hermetic).

``GET /v1/citymap/geocode``, ``GET /v1/citymap/geocode/search``,
``POST /v1/citymap/render``, ``POST /v1/citymap/import`` and
``GET /v1/citymap/results/{token}`` over **real HTTP** (in-process
uvicorn on a socket — never TestClient, per the test-design skill) with
all upstreams served from fixtures (no network, no real OSM data):

* Nominatim → synthetic gazetteer (``backend.upstream_mock.data``);
  absent names map to the router's ``city_not_found`` envelope;
* Overpass → synthetic bbox ways; OSM Main API → synthetic ``/map`` XML
  (the fallback source — a render succeeds whichever upstream answers);
* Redis → dead-port URL (memory fallback); rate limit → ``100000``.

``FUNDVISTA_UPSTREAM_MODE`` is deliberately NOT set here: the autouse
fixture installs/uninstalls the upstream-mock transport per test (it
wraps httpx client construction directly, independent of the env var),
so this file never touches the real network or a shared env.

Upstream-outage classes are stubbed at the documented fetch seams (the
module attributes ``backend.citymap.fetching.fetch_overpass`` /
``...fetch_osm_api`` and ``backend.citymap.router.geocode_city`` /
``...search_places``), never with a mock HTTP server. Failure contracts
asserted: 502 ``overpass_unavailable`` / ``nominatim_unavailable``
envelopes — never a bare text/plain 500.

Manual / ``@live`` follow-ups (cannot run hermetically): real Nominatim
1 req/s usage-policy pacing, Overpass mirror failover order, a dense
metro (full Budapest buildings) render size/latency wall-time.
"""

from __future__ import annotations

import json
import os
import re
import socket
import tempfile
import threading
import time
from urllib.parse import urlsplit

os.environ.setdefault("PENPLOT_DATA_DIR", tempfile.mkdtemp(prefix="penplot-citymap-api-"))
os.environ["PENPLOT_RATE_LIMIT"] = "100000"

import httpx
import pytest
import uvicorn

from backend import upstream_mock
from backend.citymap.geocode import GeocodeError
from backend.citymap.osm_api import OsmApiError
from backend.citymap.overpass import OverpassError
from backend.tests import redis_harness

HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Fixture-friendly bboxes (same grid the upstream_mock data.py draws):
# every corner is an exact multiple of render_snap_deg (0.002), so the
# snapped bbox equals the requested one and framing assertions stay exact.
BBOX_RENDER = (47.50, 19.00, 47.55, 19.06)   # south, west, north, east
BBOX_FALLBACK = (47.50, 19.02, 47.52, 19.05)
BBOX_IMPORT = (47.50, 19.00, 47.52, 19.03)


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
    # Pinned uvicorn has no `install_signal_handlers` Config knob; its
    # capture_signals() no-ops in a non-main thread — exactly how this
    # fixture runs the server, so no kwarg is needed.
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
            raise RuntimeError("citymap test server never became healthy")
    with httpx.Client(base_url=base, timeout=60.0) as c:
        yield c
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _clean_slate():
    """Mock upstreams + per-test cache/throttle reset (server is shared).

    The citymap stack reaches the network only through httpx, which
    ``upstream_mock.install()`` routes at the synthetic fixture edge; the
    module-scoped server keeps its state, so the citymap cache (memory
    fallback with dead Redis) is cleared around every test to keep each
    one cold and independent, plus the Nominatim throttle so cache-miss
    re-calls don't sleep out the 1 req/s production pacing.
    """
    upstream_mock.install()
    _reset_shared_state()
    yield
    _reset_shared_state()
    upstream_mock.uninstall()


def _reset_shared_state() -> None:
    from backend.citymap import geocode

    redis_harness.flush()
    geocode._reset_nominatim_throttle()


def _bbox_body(bbox: tuple[float, float, float, float], layers: list[str] | None = None, **kw) -> dict:
    south, west, north, east = bbox
    body: dict = {"bbox": {"south": south, "west": west, "north": north, "east": east}}
    if layers is not None:
        body["layers"] = layers
    body.update(kw)
    return body


# -- GET /v1/citymap/geocode --------------------------------------------------

def test_geocode_happy_szeged(live):
    resp = live.get("/v1/citymap/geocode", params={"city": "Szeged"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["city"] == "Szeged"
    assert body["display_name"] == "Szeged, Csongrád-Csanád, Hungary"
    assert body["bbox"] == {"south": 46.19, "west": 20.04, "north": 46.32, "east": 20.22}
    assert body["lat"] == pytest.approx(46.255)
    assert body["lon"] == pytest.approx(20.13)
    assert body["cache_hit"] is False  # cache cleared around every test


def test_geocode_caches_payload_in_redis(live):
    assert live.get("/v1/citymap/geocode", params={"city": "Szeged"}).status_code == 200
    cached = redis_harness.cached_values("fund-vista:citymap:v1:geocode:")
    assert cached, "geocode result was not written to Redis"
    payloads = [json.loads(value) for value in cached.values()]
    assert any(p.get("display_name") == "Szeged, Csongrád-Csanád, Hungary" for p in payloads)


def test_geocode_unknown_city_404_envelope(live):
    # One class: a valid request whose name matches nothing — blank-ish and
    # single-char queries are all "no place found", there is no 2-char rule.
    for name in ("Atlantisz Nonexistent", " "):
        resp = live.get("/v1/citymap/geocode", params={"city": name})
        assert resp.status_code == 404, (name, resp.text)
        payload = resp.json()
        assert payload["error"]["code"] == "city_not_found", payload
        assert payload["error"]["message"]


def test_geocode_empty_city_param_422(live):
    for params in ({}, {"city": ""}):
        resp = live.get("/v1/citymap/geocode", params=params)
        assert resp.status_code == 422, (params, resp.text)
        assert resp.json()["error"]["code"] == "invalid_params"


# -- GET /v1/citymap/geocode/search -------------------------------------------

def test_geocode_search_happy(live):
    resp = live.get("/v1/citymap/geocode/search", params={"city": "Dóm tér Szeged"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["city"] == "Dóm tér Szeged"
    assert body["cache_hit"] is False
    assert len(body["candidates"]) >= 1
    top = body["candidates"][0]
    assert top["display_name"].startswith("Dóm tér")  # most-specific match first
    assert top["bbox"]["south"] == pytest.approx(46.2515)
    assert top["bbox"]["west"] == pytest.approx(20.1470)
    assert top["bbox"]["north"] == pytest.approx(46.2535)
    assert top["bbox"]["east"] == pytest.approx(20.1500)
    assert top["category"] == "place" and top["type"] == "square"


def test_geocode_search_limit_boundaries(live):
    from backend.citymap import geocode

    for bad in (0, 11):
        resp = live.get("/v1/citymap/geocode/search",
                        params={"city": "szeged", "limit": bad})
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json()["error"]["code"] == "invalid_params"
    for good, expected in ((1, 1), (10, 1)):  # one Szeged row in the gazetteer
        # (the "Dóm tér" hit needs an explicit "dom ter" in the query, so it
        # only joins the results of search happy-path, not plain "szeged")
        geocode._reset_nominatim_throttle()  # cache-miss re-call, skip 1 s pacing
        resp = live.get("/v1/citymap/geocode/search",
                        params={"city": "szeged", "limit": good})
        assert resp.status_code == 200, (good, resp.text)
        assert len(resp.json()["candidates"]) == expected


def test_geocode_search_unknown_city_404(live):
    resp = live.get("/v1/citymap/geocode/search",
                    params={"city": "Atlantisz Nonexistent"})
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "city_not_found"


# -- POST /v1/citymap/render --------------------------------------------------

def test_render_happy_overpass_and_result_roundtrip(live):
    south, west, north, east = BBOX_RENDER
    resp = live.post("/v1/citymap/render", json=_bbox_body(
        BBOX_RENDER, ["roads", "buildings"]))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["city"] is None and body["display_name"] is None
    assert body["bbox"] == {"south": south, "west": west, "north": north, "east": east}
    assert body["layers"] == ["roads", "buildings"]
    assert body["path_counts"]["roads"] > 0
    assert body["raw_counts"]["ways"] >= body["path_counts"]["roads"]
    assert "osm.org/copyright" in body["attribution"]
    assert body["warnings"] == [] and body["cache_hit"] is False

    svg_url = urlsplit(body["svg_url"])
    token = svg_url.path.rsplit("/", 1)[-1]
    assert svg_url.path.startswith("/v1/citymap/results/") and HEX40.fullmatch(token)
    assert body["svg_url"].startswith(str(live.base_url))  # absolute origin link
    art = live.get(svg_url.path)  # GET /v1/citymap/results/{token}
    assert art.status_code == 200, art.text
    assert art.headers["content-type"] == "image/svg+xml"
    assert "<svg" in art.text


def test_render_caches_tiles_split_and_svg_in_redis(live):
    resp = live.post("/v1/citymap/render", json=_bbox_body(BBOX_RENDER, ["roads", "buildings"]))
    assert resp.status_code == 200, resp.text
    prefix = "fund-vista:citymap:v1:"
    # Per-tile Overpass slices, the split of them, and the rendered SVG.
    assert redis_harness.cached_values(prefix + "tile:"), "no Overpass tile slices in Redis"
    assert redis_harness.cached_values(prefix + "split:"), "no split geometry in Redis"
    svgs = redis_harness.cached_values(prefix + "svg:")
    assert len(svgs) == 1
    (svg,) = svgs.values()
    assert svg.lstrip().startswith("<svg")


def test_render_osm_api_fallback_source(live, monkeypatch):
    # Overpass down, OSM Main API (the other source) answers → still 200.
    async def _dead(*a, **k):
        raise OverpassError("mock overpass outage")

    from backend.citymap import fetching

    monkeypatch.setattr(fetching, "fetch_overpass", _dead)
    resp = live.post("/v1/citymap/render", json=_bbox_body(
        BBOX_FALLBACK, ["roads"]))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "osm_api_fallback" in body["warnings"]
    assert body["path_counts"]["roads"] > 0  # fallback data still renders


def test_render_by_city_happy(live):
    resp = live.post("/v1/citymap/render", json={
        "city": "Mockváros", "layers": ["roads"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["city"] == "Mockváros"
    assert body["display_name"] == "Mockváros, Test County, Hungary"
    assert body["bbox"] == {"south": 47.50, "west": 19.00, "north": 47.54, "east": 19.05}
    assert body["path_counts"]["roads"] > 0


def test_render_validation_bad_bodies_422(live):
    inverted = {"bbox": {"south": 47.9, "west": 19.0, "north": 47.0, "east": 19.1}}
    bad = [
        _bbox_body(BBOX_RENDER),                          # layers missing
        _bbox_body(BBOX_RENDER, []),                      # layers empty
        _bbox_body(BBOX_RENDER, ["canals"]),              # not a LayerName
        dict(_bbox_body(BBOX_RENDER, ["roads"]), city="Budapest"),  # both city+bbox
        {"city": "", "layers": ["roads"]},                # blank city
        inverted | {"layers": ["roads"]},                 # south >= north
        dict(_bbox_body(BBOX_RENDER, ["roads"]), nosuch=1),  # extra=forbid
        dict(_bbox_body(BBOX_RENDER, ["roads"]), bearing_deg=90.0),  # needs aspect
    ]
    for payload in bad:
        resp = live.post("/v1/citymap/render", json=payload)
        assert resp.status_code == 422, (payload, resp.text)
        assert resp.json()["error"]["code"] == "invalid_params"


def test_render_bbox_span_guard_422(live):
    # 0.9 deg per side > max_bbox_deg 0.8 — documented area guard, envelope
    # shape instead of a bare 500.
    resp = live.post("/v1/citymap/render",
                     json=_bbox_body((47.0, 19.0, 47.9, 19.1), ["roads"]))
    assert resp.status_code == 422, resp.text
    payload = resp.json()
    assert payload["error"]["code"] == "invalid_params"
    assert "Area too large" in payload["error"]["message"]


def test_render_unknown_city_404(live):
    resp = live.post("/v1/citymap/render",
                     json={"city": "Atlantisz Nonexistent", "layers": ["roads"]})
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "city_not_found"


def test_render_upstream_outage_502_envelope(live, monkeypatch):
    # Overpass AND its OSM Main API fallback both dead at the fetch seam →
    # the documented 502 `overpass_unavailable` envelope, never a bare
    # text/plain 500.
    async def _dead_overpass(*a, **k):
        raise OverpassError("mock overpass outage")

    async def _dead_osm_api(*a, **k):
        raise OsmApiError("mock osm api outage")

    from backend.citymap import fetching

    monkeypatch.setattr(fetching, "fetch_overpass", _dead_overpass)
    monkeypatch.setattr(fetching, "fetch_osm_api", _dead_osm_api)
    resp = live.post("/v1/citymap/render", json=_bbox_body(BBOX_RENDER, ["roads"]))
    assert resp.status_code == 502, resp.text
    payload = resp.json()
    assert payload["error"]["code"] == "overpass_unavailable"
    assert "mock overpass outage" in payload["error"]["message"]
    assert "mock osm api outage" in payload["error"]["message"]


def test_geocode_nominatim_outage_502_envelope(live, monkeypatch):
    # Nominatim dead at the geocode seam → 502 `nominatim_unavailable` on
    # every endpoint that resolves place names.
    async def _dead(*a, **k):
        raise GeocodeError("mock nominatim outage")

    monkeypatch.setattr("backend.citymap.router.geocode_city", _dead)
    monkeypatch.setattr("backend.citymap.router.search_places", _dead)
    resp = live.get("/v1/citymap/geocode", params={"city": "Szeged"})
    assert resp.status_code == 502, resp.text
    payload = resp.json()
    assert payload["error"]["code"] == "nominatim_unavailable"
    assert "mock nominatim outage" in payload["error"]["message"]
    resp = live.get("/v1/citymap/geocode/search", params={"city": "Szeged"})
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "nominatim_unavailable"
    resp = live.post("/v1/citymap/render", json={"city": "Szeged", "layers": ["roads"]})
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "nominatim_unavailable"


# -- POST /v1/citymap/import --------------------------------------------------

def test_import_registers_image_and_get_images(live):
    south, west, north, east = BBOX_IMPORT
    resp = live.post("/v1/citymap/import", json=_bbox_body(
        BBOX_IMPORT, ["roads", "buildings"]))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    image_id = body["image_id"]
    assert HEX64.fullmatch(image_id)
    assert body["city"] is None
    assert body["bbox"] == {"south": south, "west": west, "north": north, "east": east}
    assert body["layers"] == ["roads", "buildings"]
    assert body["path_counts"]["roads"] > 0
    assert "osm.org/copyright" in body["attribution"]

    # The image really landed in the penplot store: GET /v1/images/{id} 200s.
    meta = live.get(f"/v1/images/{image_id}")
    assert meta.status_code == 200, meta.text
    mint = meta.json()
    assert mint["image_id"] == image_id
    assert mint["format"] == "svg" and mint["is_vector"] is True
    assert mint["width"] > 0 and mint["height"] > 0


# -- GET /v1/citymap/results/{token} ------------------------------------------

def test_results_token_guards(live):
    # Malformed (non-hex) and a well-formed unknown token both map to the
    # 404 result envelope — an expired or rerun render.
    for token in ("not-hex", "z" * 40):
        resp = live.get(f"/v1/citymap/results/{token}")
        assert resp.status_code == 404, (token, resp.text)
        assert resp.json()["error"]["code"] == "result_not_found"
    resp = live.get("/v1/citymap/results/" + "0" * 40)
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "result_not_found"


def test_validation_failures_never_rate_limit_confusion(live):
    # Validation failures must be 422 envelopes even when hammering; the
    # penplot rate-limit bucket is shared and must not produce 429 here.
    for _ in range(3):
        r = live.get("/v1/citymap/geocode")
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_params", r.text
        r = live.post("/v1/citymap/render", json={"bogus": True})
        assert r.status_code == 422, r.text


def test_render_overpass_remark_caches_no_tiles(live, monkeypatch):
    # Overpass answers 200 + a timeout remark with no elements; the render
    # must fail over (here: OSM API dead too → 502) and must not pin the
    # blank result as "covered, empty" tiles in Redis.
    from backend.citymap import fetching, overpass
    from backend.citymap.config import settings

    class _Resp:
        status_code = 200

        def json(self):
            return {"elements": [], "remark": "runtime error: Query timed out"}

    class _Client:
        async def post(self, *a, **k):
            return _Resp()

        async def aclose(self):
            return None

    async def _remark_overpass(query, client=None):
        return await overpass.fetch_overpass(query, client=_Client())

    async def _dead_osm_api(*a, **k):
        raise OsmApiError("mock osm api outage")

    monkeypatch.setattr(settings, "overpass_retries", 0)
    monkeypatch.setattr(fetching, "fetch_overpass", _remark_overpass)
    monkeypatch.setattr(fetching, "fetch_osm_api", _dead_osm_api)
    resp = live.post("/v1/citymap/render", json=_bbox_body(BBOX_RENDER, ["roads"]))
    assert resp.status_code == 502, resp.text
    assert not redis_harness.cached_values("fund-vista:citymap:v1:tile:")
