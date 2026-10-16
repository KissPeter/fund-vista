"""Upstream mock mode (FUNDVISTA_UPSTREAM_MODE=mock): fixtures + fail-closed.

Drives the real production fetchers/parsers against the mock network edge.
No sockets: the httpx clients get a MockTransport via ``upstream_mock``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io

import httpx
import pytest

from backend import upstream_mock
from backend.airports import ourairports
from backend.airports.overpass import (
    build_airport_query,
    build_context_query,
    fetch_airport_polygons,
    split_aeroway,
    split_context,
)
from backend.citymap import geocode
from backend.citymap.osm_api import fetch_osm_api
from backend.citymap.overpass import (
    build_overpass_query,
    fetch_overpass,
    split_elements,
)
from backend.traffic import availability, ingest

BBOX = (47.50, 19.00, 47.55, 19.06)  # south, west, north, east


@pytest.fixture(autouse=True)
def mock_mode():
    upstream_mock.install()
    yield
    upstream_mock.uninstall()


def run(coro):
    return asyncio.run(coro)


def test_env_switch_is_read_from_environment(monkeypatch):
    monkeypatch.delenv(upstream_mock.ENV_VAR, raising=False)
    assert not upstream_mock.enabled()
    monkeypatch.setenv(upstream_mock.ENV_VAR, "MOCK")
    assert upstream_mock.enabled()


def test_install_is_idempotent_and_reversible():
    upstream_mock.install()  # second call is a no-op
    upstream_mock.uninstall()
    # Stock client again: an unmocked URL is no longer intercepted (build only,
    # nothing is sent).
    assert not hasattr(httpx.Client.__init__, "__wrapped__")
    upstream_mock.install()


def test_unmocked_host_fails_closed_not_as_httpx_error():
    async def go():
        async with httpx.AsyncClient() as client:
            await client.get("https://example.com/anything")

    with pytest.raises(upstream_mock.UpstreamNotMocked) as exc:
        run(go())
    assert "example.com" in str(exc.value)
    assert not isinstance(exc.value, httpx.HTTPError)


def test_explicit_transport_is_left_alone():
    transport = httpx.MockTransport(lambda r: httpx.Response(204))

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return (await client.get("https://example.com/x")).status_code

    assert run(go()) == 204


def test_citymap_overpass_layers_follow_the_query():
    query = build_overpass_query(BBOX, ["highways", "rails"])
    elements = run(fetch_overpass(query))
    geoms, counts = split_elements(elements, ["highways", "rails", "roads"])
    assert len(geoms["highways"]) == 1
    assert len(geoms["rails"]) == 1
    assert geoms["roads"] == []  # not requested -> not served
    assert counts["ways"] == 2
    again = run(fetch_overpass(query))
    assert again == elements  # deterministic


def test_citymap_overpass_stays_inside_the_bbox():
    query = build_overpass_query(BBOX, ["roads", "buildings", "water", "waterway", "paths"])
    elements = run(fetch_overpass(query))
    geoms, _ = split_elements(elements, ["roads", "buildings", "water", "waterway", "paths"])
    s, w, n, e = BBOX
    for pts in (p for layer in geoms.values() for p in layer):
        for lon, lat in pts:
            assert s <= lat <= n and w <= lon <= e


def test_osm_api_fallback_parses_into_layers():
    elements = run(fetch_osm_api(BBOX))
    geoms, _ = split_elements(elements, ["highways", "roads", "rails", "buildings"])
    assert geoms["highways"] and geoms["roads"] and geoms["rails"] and geoms["buildings"]


def test_nominatim_geocode_and_search():
    top = run(geocode.geocode_city("Szeged"))
    s, w, n, e = top["bbox"]
    assert s < n and w < e
    found = run(geocode.search_places("Dóm tér Szeged", limit=5))
    assert found["candidates"][0]["display_name"].startswith("Dóm tér")
    with pytest.raises(geocode.CityNotFoundError):
        run(geocode.geocode_city("Atlantisz Nonexistent"))


def test_ourairports_resolves_runways_and_frequencies():
    airport, _ = run(ourairports.resolve_airport("LHBP"))
    assert airport["ident"] == "LHBP"
    runways, _ = run(ourairports.airport_runways("LHBP"))
    assert runways
    freqs, _ = run(ourairports.airport_frequencies("LHBP"))
    assert freqs
    hits, _ = run(ourairports.search_airports("Debrecen"))
    assert any(h["icao"] == "LHDC" for h in hits)


def test_airport_overpass_geom_and_context():
    lat, lon = 47.4369, 19.2556
    geoms, counts = split_aeroway(run(fetch_airport_polygons(build_airport_query(lat, lon, 3000))))
    assert geoms["runway"] and geoms["taxiway"] and geoms["apron"] and geoms["terminal"]
    assert counts["ways"] >= 5
    ctx = split_context(run(fetch_airport_polygons(build_context_query(lat, lon, 3000))))
    assert ctx["highways"] and ctx["roads"] and ctx["rails"] and ctx["buildings"]


def test_traffic_release_index_and_archive_round_trip():
    day = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=3)

    async def status():
        async with httpx.AsyncClient() as client:
            return await availability.asource_status(day, client)

    st = run(status())
    assert st["status"] == "available" and st["parts"] == 1

    with httpx.Client() as client:
        urls = ingest.asset_urls(day, client)
        stream = io.BufferedReader(ingest.ChunkReader(ingest._chunks(urls, client)), 1 << 20)
        spec = {"icao": "LHBP", "lat": 47.4369, "lon": 19.2556, "elev_ft": 495.0,
                "bbox": ingest.default_bbox(47.4369, 19.2556)}
        flights = ingest.extract_multi(stream, [spec])["LHBP"]
    assert len(flights) == 2  # one arrival + one departure
    assert {f["calls"][0][1].strip()[:3] for f in flights} == {"WZZ", "RYR"}
