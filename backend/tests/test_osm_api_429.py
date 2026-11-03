"""OSM Main API 429: park the endpoint for the server's hint, never retry."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from backend.citymap import osm_api
from backend.citymap.config import settings
from backend.citymap.osm_api import OsmApiError, fetch_osm_api
from backend.http.mirror_health import osm_api_mirrors

BBOX = (47.50, 19.00, 47.52, 19.02)  # one cell
BODY = "You have downloaded too much data. Please try again in 37 seconds."
OK_XML = '<osm><node id="1" lat="47.5" lon="19.0"/></osm>'


@pytest.fixture(autouse=True)
def _fresh_park():
    osm_api_mirrors.reset()
    yield
    osm_api_mirrors.reset()


def _client(handler):
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped)), calls


async def _seconds_left() -> float:
    _, parked = await osm_api_mirrors.split([settings.osm_api_url])
    return parked[0][1] if parked else 0.0


def test_429_hint_from_retry_after_header():
    client, calls = _client(
        lambda r: httpx.Response(429, headers={"Retry-After": "120"}, text=BODY)
    )

    async def scenario():
        with pytest.raises(OsmApiError, match="429"):
            await fetch_osm_api(BBOX, client)
        return await _seconds_left()

    left = asyncio.run(scenario())
    assert len(calls) == 1  # never retried in place
    assert 118 <= left <= 120  # header wins over the 37s in the body


def test_429_hint_from_body_text():
    client, calls = _client(lambda r: httpx.Response(429, text=BODY))

    async def scenario():
        with pytest.raises(OsmApiError):
            await fetch_osm_api(BBOX, client)
        return await _seconds_left()

    left = asyncio.run(scenario())
    assert len(calls) == 1
    assert 35 <= left <= 37


def test_429_default_cooldown_without_hint():
    client, _ = _client(lambda r: httpx.Response(429, text="slow down"))

    async def scenario():
        with pytest.raises(OsmApiError):
            await fetch_osm_api(BBOX, client)
        return await _seconds_left()

    left = asyncio.run(scenario())
    assert settings.osm_api_cooldown_s - 2 <= left <= settings.osm_api_cooldown_s


def test_429_hint_clamped_to_max_cooldown():
    client, _ = _client(
        lambda r: httpx.Response(429, headers={"Retry-After": "86400"})
    )

    async def scenario():
        with pytest.raises(OsmApiError):
            await fetch_osm_api(BBOX, client)
        return await _seconds_left()

    left = asyncio.run(scenario())
    assert settings.osm_api_max_cooldown_s - 2 <= left <= settings.osm_api_max_cooldown_s


def test_parked_endpoint_fails_fast_without_network():
    client, calls = _client(lambda r: httpx.Response(429, text=BODY))

    async def scenario():
        with pytest.raises(OsmApiError):
            await fetch_osm_api(BBOX, client)
        assert len(calls) == 1
        with pytest.raises(OsmApiError, match=r"parked .* another \d+s"):
            await fetch_osm_api(BBOX, client)

    asyncio.run(scenario())
    assert len(calls) == 1  # second call never touched the network


def test_unparked_endpoint_still_works_after_park_expires(monkeypatch):
    client, calls = _client(lambda r: httpx.Response(200, text=OK_XML))

    async def scenario():
        await osm_api_mirrors.park(settings.osm_api_url, 30)
        with pytest.raises(OsmApiError):
            await fetch_osm_api(BBOX, client)
        assert calls == []
        osm_api_mirrors.reset()
        return await fetch_osm_api(BBOX, client)

    elements = asyncio.run(scenario())
    assert elements and elements[0]["type"] == "node"
    assert len(calls) == 1


def test_400_still_subdivides(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        bbox = [float(v) for v in request.url.params["bbox"].split(",")]
        if bbox[2] - bbox[0] > 0.011:
            return httpx.Response(400, text="too many nodes")
        return httpx.Response(200, text=OK_XML)

    client, calls = _client(handler)
    monkeypatch.setattr(settings, "osm_api_min_deg", 0.005)
    elements = asyncio.run(fetch_osm_api(BBOX, client))
    assert elements
    assert len(calls) == 5  # 1 dense cell + 4 quadrants
    assert osm_api.osm_api_mirrors is osm_api_mirrors
