"""Data-provider ToS compliance (issue #59): hermetic, no network.

Covers the Nominatim 1 req/s throttle + identification, the Overpass
fair-use guards (UA, sequential fallback, fail-fast, concurrency cap),
the OSM API small-read shape, the OurAirports CC0 fetch shape, and the D3
attribution placement (API/UI metadata, never plotted artwork).

All cases are hermetic: upstreams are fakes, timing is asserted via state
manipulation + recorded sleeps (never real ``sleep()``), and no live
server is needed.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import backend.airports.overpass as airports_overpass
import backend.citymap.geocode as geocode
import backend.citymap.overpass as city_overpass
from backend.airports import cache as airports_cache_mod
from backend.airports.config import settings as airports_settings
from backend.citymap.config import settings as citymap_settings


# -- config defaults ---------------------------------------------------------

def test_nominatim_policy_knobs():
    assert citymap_settings.nominatim_min_interval_s >= 1.0
    assert "fund-vista" in citymap_settings.nominatim_referer
    assert citymap_settings.nominatim_referer.startswith("https://")
    assert "fund-vista" in citymap_settings.user_agent
    assert "http" in citymap_settings.user_agent  # contact URL, not bare name
    assert citymap_settings.nominatim_timeout_s <= 30.0


def test_overpass_mirror_sets_and_fair_use_knobs():
    for settings in (citymap_settings, airports_settings):
        assert len(settings.overpass_urls) >= 2
        for url in settings.overpass_urls:
            assert url.startswith("https://"), url
            assert url.endswith("/interpreter"), url
        assert settings.overpass_timeout_s <= 60.0
        assert 1 <= settings.overpass_max_concurrent <= 5
        assert settings.overpass_retries <= 2
    # Both modules point at the same public infra.
    assert citymap_settings.overpass_urls == airports_settings.overpass_urls
    assert "fund-vista" in airports_settings.user_agent
    assert "http" in airports_settings.user_agent  # contact URL


# -- Nominatim identification ------------------------------------------------

def test_geocode_headers_identify_app():
    headers = geocode._headers()
    assert headers["User-Agent"] == citymap_settings.user_agent
    assert headers["Referer"] == citymap_settings.nominatim_referer
    assert "fund-vista" in headers["Referer"]
    assert headers["Accept"] == "application/json"


# -- Nominatim throttle ------------------------------------------------------

def test_throttle_first_call_free_second_waits(monkeypatch):
    async def scenario():
        geocode._reset_nominatim_throttle()
        assert await geocode._throttle_nominatim() == 0.0
        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        waited = await geocode._throttle_nominatim()
        assert len(sleeps) == 1
        assert waited == pytest.approx(sleeps[0], abs=0.25)
        assert 0.0 < waited <= citymap_settings.nominatim_min_interval_s + 0.01
        geocode._reset_nominatim_throttle()

    asyncio.run(scenario())


def test_geocode_cache_hit_skips_throttle_and_upstream(monkeypatch):
    """A cached answer must not pace nor touch HTTP (hits keep us compliant)."""
    cached_payload = {
        "display_name": "Budapest", "bbox": (47.4, 19.0, 47.6, 19.3),
        "lat": 47.5, "lon": 19.1,
    }

    async def fake_get(_key: str):
        return json.dumps(cached_payload)

    async def boom_set(*_a, **_k):
        raise AssertionError("cache hit must not write")

    throttle_calls: list[float] = []

    async def fake_throttle() -> float:
        throttle_calls.append(1.0)
        return 0.0

    def boom_client(*_a, **_k):
        raise AssertionError("cache hit must not open HTTP")

    monkeypatch.setattr(geocode, "cache_get", fake_get)
    monkeypatch.setattr(geocode, "cache_set", boom_set)
    monkeypatch.setattr(geocode, "_throttle_nominatim", fake_throttle)
    monkeypatch.setattr(geocode.httpx, "AsyncClient", boom_client)

    async def scenario():
        geocode._reset_nominatim_throttle()
        out = await geocode.geocode_city("Budapest")
        assert out["cache_hit"] is True
        assert out["display_name"] == "Budapest"
        out2 = await geocode.search_places("Budapest", 5)
        assert out2["cache_hit"] is True

    asyncio.run(scenario())
    assert throttle_calls == []


class _NominatimResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _NominatimClient:
    """Async-context-manager fake for httpx.AsyncClient (geocode shape)."""

    instances: list[_NominatimClient] = []
    calls: list[tuple] = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        _NominatimClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        _NominatimClient.calls.append((url, dict(params or {}), dict(self.kwargs.get("headers", {}))))
        return _NominatimResp([{
            "boundingbox": ["47.4", "47.6", "19.0", "19.3"],
            "display_name": "Budapest", "lat": "47.5", "lon": "19.1",
            "class": "place", "type": "city",
        }])


def test_geocode_miss_throttles_then_identifies(monkeypatch):
    """Cache miss → exactly one paced upstream GET with UA + Referer."""
    _NominatimClient.instances.clear()
    _NominatimClient.calls.clear()

    async def fake_get(_key: str):
        return None

    stored: dict[str, str] = {}

    async def fake_set(key: str, value: str, ttl_s=None):
        stored[key] = value
        return True

    throttle_calls: list[bool] = []

    async def fake_throttle() -> float:
        throttle_calls.append(True)
        return 0.0

    monkeypatch.setattr(geocode, "cache_get", fake_get)
    monkeypatch.setattr(geocode, "cache_set", fake_set)
    monkeypatch.setattr(geocode, "_throttle_nominatim", fake_throttle)
    monkeypatch.setattr(geocode.httpx, "AsyncClient", _NominatimClient)
    geocode._reset_nominatim_throttle()

    async def scenario():
        out = await geocode.geocode_city("Budapest")
        assert out["cache_hit"] is False
        assert out["bbox"] == (47.4, 19.0, 47.6, 19.3)

    asyncio.run(scenario())
    assert throttle_calls == [True]  # paced exactly once, before the GET
    assert len(_NominatimClient.calls) == 1
    url, params, headers = _NominatimClient.calls[0]
    assert url == citymap_settings.nominatim_url
    assert params["q"] == "Budapest"
    assert headers["User-Agent"] == citymap_settings.user_agent
    assert headers["Referer"] == citymap_settings.nominatim_referer
    assert stored  # miss is cached for next time
    geocode._reset_nominatim_throttle()


# -- Overpass: fallback order, fail-fast, UA, concurrency cap ----------------

class _OverpassResp:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    @property
    def text(self):
        return f"fake body {self.status_code}"


class _OverpassClient:
    """Minimal fake for the ``client.post`` surface fetchers use."""

    def __init__(self, script: list[_OverpassResp]):
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url, data=None, headers=None):
        self.calls.append((url, dict(headers or {})))
        idx = min(len(self.calls) - 1, len(self.script) - 1)
        return self.script[idx]


def test_citymap_overpass_falls_over_in_configured_order(monkeypatch):
    async def scenario():
        monkeypatch.setattr(citymap_settings, "overpass_retries", 0)
        monkeypatch.setattr(citymap_settings, "overpass_max_concurrent", 2)
        city_overpass._reset_overpass_semaphore()
        elements = [{"type": "node", "id": 1}]
        client = _OverpassClient([
            _OverpassResp(504),
            _OverpassResp(200, {"elements": elements}),
        ])
        out = await city_overpass.fetch_overpass("data", client=client)  # type: ignore[arg-type]
        return out, client

    out, client = asyncio.run(scenario())
    assert out == [{"type": "node", "id": 1}]
    assert [url for url, _ in client.calls] == list(citymap_settings.overpass_urls[:2])
    for _, headers in client.calls:
        assert headers["User-Agent"] == citymap_settings.user_agent


def test_citymap_overpass_fail_fast_on_403_no_same_mirror_retry(monkeypatch):
    """403 (blocked) must not be retried on the same mirror — fail over."""
    async def scenario():
        monkeypatch.setattr(citymap_settings, "overpass_retries", 1)
        monkeypatch.setattr(citymap_settings, "overpass_max_concurrent", 2)
        city_overpass._reset_overpass_semaphore()

        async def boom_sleep(_delay: float) -> None:
            raise AssertionError("403 must not back off / retry")

        monkeypatch.setattr(asyncio, "sleep", boom_sleep)
        elements = [{"type": "node", "id": 7}]
        client = _OverpassClient([
            _OverpassResp(403),
            _OverpassResp(200, {"elements": elements}),
        ])
        out = await city_overpass.fetch_overpass("data", client=client)  # type: ignore[arg-type]
        return out, client

    out, client = asyncio.run(scenario())
    assert out == [{"type": "node", "id": 7}]
    assert [url for url, _ in client.calls] == list(citymap_settings.overpass_urls[:2])


def test_airports_overpass_same_failover_shape(monkeypatch):
    async def scenario():
        monkeypatch.setattr(airports_settings, "overpass_retries", 0)
        monkeypatch.setattr(airports_settings, "overpass_max_concurrent", 2)
        airports_overpass._reset_overpass_semaphore()
        elements = [{"type": "way", "id": 3}]
        client = _OverpassClient([
            _OverpassResp(500),
            _OverpassResp(200, {"elements": elements}),
        ])
        out = await airports_overpass.fetch_airport_polygons("data", client=client)  # type: ignore[arg-type]
        return out, client

    out, client = asyncio.run(scenario())
    assert out == [{"type": "way", "id": 3}]
    assert [url for url, _ in client.calls] == list(airports_settings.overpass_urls[:2])
    for _, headers in client.calls:
        assert headers["User-Agent"] == airports_settings.user_agent


def test_overpass_semaphore_tracks_setting(monkeypatch):
    monkeypatch.setattr(citymap_settings, "overpass_max_concurrent", 1)
    city_overpass._reset_overpass_semaphore()
    assert city_overpass._overpass_semaphore()._value == 1
    monkeypatch.setattr(citymap_settings, "overpass_max_concurrent", 3)
    assert city_overpass._overpass_semaphore()._value == 3
    city_overpass._reset_overpass_semaphore()

    monkeypatch.setattr(airports_settings, "overpass_max_concurrent", 2)
    airports_overpass._reset_overpass_semaphore()
    assert airports_overpass._overpass_semaphore()._value == 2
    airports_overpass._reset_overpass_semaphore()


# -- OSM API small-read shape -------------------------------------------------

def test_osm_api_sends_ua_and_400_means_subdivide(monkeypatch):
    from backend.citymap import osm_api
    from backend.citymap.osm_api import _DenseCellError

    seen: list[dict] = []

    class FakeResp:
        def __init__(self, status: int, text: str = ""):
            self.status_code = status
            self.text = text

    class FakeClient:
        def __init__(self, resp: FakeResp):
            self.resp = resp

        async def get(self, url, params=None, headers=None):
            seen.append({"url": url, "params": params, "headers": dict(headers or {})})
            return self.resp

    async def scenario():
        monkeypatch.setattr(osm_api.settings, "osm_api_retries", 0)
        cell = (47.5, 19.1, 47.55, 19.15)
        with pytest.raises(_DenseCellError):
            await osm_api._get_cell_elements(
                cell, FakeClient(FakeResp(400, "too many nodes"))  # type: ignore[arg-type]
            )
        good_xml = (
            '<?xml version="1.0"?><osm version="0.6">'
            '<node id="1" lat="47.5" lon="19.1"/></osm>'
        )
        elements = await osm_api._get_cell_elements(
            cell, FakeClient(FakeResp(200, good_xml))  # type: ignore[arg-type]
        )
        return elements

    elements = asyncio.run(scenario())
    assert elements == [{"type": "node", "id": 1, "lon": 19.1, "lat": 47.5}]
    assert seen
    for call in seen:
        assert call["url"] == citymap_settings.osm_api_url
        assert call["headers"]["User-Agent"] == citymap_settings.user_agent


# -- OurAirports CC0 fetch shape ----------------------------------------------

def test_ourairports_sends_ua_and_caches(monkeypatch):
    from backend.airports import ourairports

    store: dict[str, str] = {}

    async def fake_get(_key: str):
        return store.get(_key)

    async def fake_set(key: str, value: str, ttl_s=None):
        store[key] = value
        return True

    monkeypatch.setattr(airports_cache_mod, "cache_get", fake_get)
    monkeypatch.setattr(airports_cache_mod, "cache_set", fake_set)

    http_calls: list[dict] = []

    class FakeResp:
        status_code = 200
        text = "ident,name\nLHBP,Budapest\n"

    class FakeClient:
        async def get(self, url, headers=None):
            http_calls.append({"url": url, "headers": dict(headers or {})})
            return FakeResp()

    async def scenario():
        first, hit1 = await ourairports._fetch_csv("airports", FakeClient())  # type: ignore[arg-type]
        second, hit2 = await ourairports._fetch_csv("airports", FakeClient())  # type: ignore[arg-type]
        return first, hit1, second, hit2

    first, hit1, second, hit2 = asyncio.run(scenario())
    assert hit1 is False and hit2 is True
    assert first == second and "LHBP" in first
    assert len(http_calls) == 1  # second serve comes from cache
    assert http_calls[0]["url"].endswith("/airports.csv")
    assert http_calls[0]["headers"]["User-Agent"] == airports_settings.user_agent


# -- D3 attribution placement --------------------------------------------------

def test_attribution_in_metadata_never_in_artwork():
    from backend.airports.render import render_diagram
    from backend.airports.schemas import ATTRIBUTION as AIR_ATTRIBUTION
    from backend.citymap.render import render_svg
    from backend.citymap.schemas import ATTRIBUTION as CITY_ATTRIBUTION

    assert "OpenStreetMap" in CITY_ATTRIBUTION
    assert "ODbL" in CITY_ATTRIBUTION
    assert "osm.org/copyright" in CITY_ATTRIBUTION
    assert "OurAirports" in AIR_ATTRIBUTION
    assert "CC0" in AIR_ATTRIBUTION
    assert "OpenStreetMap" in AIR_ATTRIBUTION

    svg, _ = render_svg(
        {"roads": [[(19.0, 47.5), (19.1, 47.5)]]},
        (47.4, 19.0, 47.6, 19.3), ["roads"],
    )
    assert "<text" not in svg and "<!--" not in svg
    assert "OpenStreetMap" not in svg

    airport = {
        "ident": "LHBP", "name": "Budapest", "latitude_deg": 47.4369,
        "longitude_deg": 19.2556,
    }
    runway = {
        "le_ident": "13L", "he_ident": "31R", "length_ft": "3000",
        "width_ft": "150", "le_latitude_deg": "47.4395",
        "le_longitude_deg": "19.2510", "he_latitude_deg": "47.4342",
        "he_longitude_deg": "19.2602",
    }
    osm = {"runway": [], "taxiway": [[(19.252, 47.438), (19.259, 47.435)]],
           "apron": [], "terminal": [], "hangar": []}
    svg2, _, _, _ = render_diagram(
        airport=airport, runways=[runway], frequencies=[], osm_geoms=osm,
        width=1000,
    )
    assert "OurAirports" not in svg2 and "OpenStreetMap" not in svg2
    assert 'id="footer"' not in svg2
