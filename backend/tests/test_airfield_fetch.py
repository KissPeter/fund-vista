"""Airfield fetch wiring (F-008): cache, error mapping, context path, basemap.

No network. ``load_airfield`` is replaced in the modules that call it, and
the Redis cache falls back to memory (``configure_airports_redis(None)``).
The routing rules of ``sources.load_airfield`` itself are covered in
``test_airfield_sources.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import backend.airports.fetching as fetching
from backend.airports.cache import cache_get, configure_airports_redis
from backend.airports.config import settings
from backend.airports.overpass import (
    AEROWAY_CLASSES,
    AirportOverpassError,
    build_context_query,
)
from backend.airports.render import _build_svg, airfield_raw_counts
from backend.airports.sources import AirfieldResult
from backend.traffic import basemap as bm
from backend.traffic import cache as traffic_cache

LAT, LON, R = 47.4369, 19.2556, 3000.0


@pytest.fixture(autouse=True)
def _memory_cache():
    configure_airports_redis(None)
    yield


def _airfield(warnings=(), release="2026-10-21.0") -> AirfieldResult:
    geoms = {cls: [] for cls in AEROWAY_CLASSES}
    geoms["runway"] = [[(19.2510, 47.4395), (19.2602, 47.4342)]]
    geoms["taxiway"] = [[(19.2520, 47.4380), (19.2590, 47.4350)]]
    geoms["apron"] = [[(19.2550, 47.4370), (19.2570, 47.4370),
                       (19.2570, 47.4360), (19.2550, 47.4370)]]
    geoms["terminal"] = [[(19.2540, 47.4372), (19.2548, 47.4376)]]
    return AirfieldResult(
        geoms=geoms,
        taxiway_refs=[("A1", [(19.2520, 47.4380), (19.2590, 47.4350)])],
        sources={
            "runway": "overture", "taxiway": "overture",
            "apron": "overture", "stopways": "overture",
            "terminal": "osm", "hangar": "osm", "stands": "osm",
        },
        warnings=list(warnings),
        overture_release=release,
    )


def _stub_load(monkeypatch, result: AirfieldResult | Exception, calls: list):
    async def fake(lat, lon, radius_m, *, client=None):
        calls.append((lat, lon, radius_m))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(fetching, "load_airfield", fake)


def test_cache_hit_returns_identical_airfield(monkeypatch):
    fresh = _airfield()
    calls: list = []
    _stub_load(monkeypatch, fresh, calls)

    w1: list[str] = []
    w2: list[str] = []
    r1, e1 = asyncio.run(fetching._load_airfield(LAT, LON, R, w1))
    r2, e2 = asyncio.run(fetching._load_airfield(LAT, LON, R, w2))

    assert e1 is None and e2 is None
    assert len(calls) == 1, "a repeat must not reload the airfield"
    assert r1 == fresh
    assert r2 == r1
    assert "airfield_cache_hit" in w2 and "airfield_cache_hit" not in w1


def test_cache_key_includes_overture_release_and_switch(monkeypatch):
    monkeypatch.setattr(settings, "overture_enabled", True)
    monkeypatch.setattr(settings, "overture_release", "2026-10-21.0")
    key = fetching._airfield_cache_key(LAT, LON, R)
    assert key != fetching._airfield_cache_key(LAT, LON, R + 500)

    monkeypatch.setattr(settings, "overture_release", "2026-11-18.0")
    assert fetching._airfield_cache_key(LAT, LON, R) != key

    monkeypatch.setattr(settings, "overture_enabled", False)
    off = fetching._airfield_cache_key(LAT, LON, R)
    assert off != key


def test_result_missing_osm_layers_is_served_but_not_cached(monkeypatch):
    degraded = _airfield(warnings=["osm_layers_unavailable"])
    _stub_load(monkeypatch, degraded, [])

    warnings: list[str] = []
    result, err = asyncio.run(fetching._load_airfield(LAT, LON, R, warnings))

    assert err is None and result == degraded
    assert "osm_layers_unavailable" in warnings
    stored = asyncio.run(cache_get(fetching._airfield_cache_key(LAT, LON, R)))
    assert stored is None


def test_overture_fallback_is_served_but_not_cached(monkeypatch):
    """A degraded result is never cached: the next request retries Overture."""
    fallback = _airfield(warnings=["overture_unavailable"])
    calls: list = []
    _stub_load(monkeypatch, fallback, calls)

    asyncio.run(fetching._load_airfield(LAT, LON, R, []))
    warnings: list[str] = []
    again, err = asyncio.run(fetching._load_airfield(LAT, LON, R, warnings))

    assert err is None and again == fallback
    assert len(calls) == 2
    assert "overture_unavailable" in warnings
    assert "airfield_cache_hit" not in warnings
    stored = asyncio.run(cache_get(fetching._airfield_cache_key(LAT, LON, R)))
    assert stored is None


def test_overture_empty_fallback_is_cached(monkeypatch):
    """A complete Overpass fallback after an empty Overture read is not degraded."""
    complete = _airfield(warnings=["overture_empty"])
    calls: list = []
    _stub_load(monkeypatch, complete, calls)

    asyncio.run(fetching._load_airfield(LAT, LON, R, []))
    warnings: list[str] = []
    again, _ = asyncio.run(fetching._load_airfield(LAT, LON, R, warnings))

    assert again == complete
    assert len(calls) == 1
    assert "airfield_cache_hit" in warnings


def test_render_key_follows_overture_switch_release_and_source_version(monkeypatch):
    from backend.airports import render_cache
    from backend.airports.schemas import RenderRequest as ARequest

    body = ARequest(icao="LHBP")
    monkeypatch.setattr(settings, "overture_enabled", True)
    monkeypatch.setattr(settings, "overture_release", "2026-10-21.0")
    base, _ = render_cache._render_keys("LHBP", 3500.0, body)

    monkeypatch.setattr(settings, "overture_enabled", False)
    assert render_cache._render_keys("LHBP", 3500.0, body)[0] != base
    monkeypatch.setattr(settings, "overture_enabled", True)

    monkeypatch.setattr(settings, "overture_release", "2026-11-18.0")
    assert render_cache._render_keys("LHBP", 3500.0, body)[0] != base
    monkeypatch.setattr(settings, "overture_release", "2026-10-21.0")

    monkeypatch.setattr(render_cache, "SOURCES_VERSION", "2")
    assert render_cache._render_keys("LHBP", 3500.0, body)[0] != base
    monkeypatch.setattr(render_cache, "SOURCES_VERSION", "1")

    assert render_cache._render_keys("LHBP", 3500.0, body)[0] == base


def test_degraded_render_is_built_but_not_stored_by_the_request_path(monkeypatch):
    import backend.airports.router as airports_router
    from backend.airports.schemas import RenderRequest as ARequest

    airport = {"ident": "LHBP", "name": "Budapest", "latitude_deg": LAT,
               "longitude_deg": LON}
    stored: list = []

    async def fake_build(airport, runway_rows, freq_rows, lat, lon, radius_m,
                         body, warnings, **kwargs):
        warnings.append("osm_layers_unavailable")
        return "<svg/>", {"runway": 1}, {"ways": 1}, 0.0, None, "ctx"

    async def record_store(*args, **kwargs):
        stored.append(args)

    async def no_splits(*args, **kwargs):
        return None

    monkeypatch.setattr(airports_router, "_render_uncached", fake_build)
    monkeypatch.setattr(airports_router, "_store_render", record_store)
    monkeypatch.setattr(airports_router, "_store_splits", no_splits)

    body = ARequest(icao="LHBP")
    warnings: list[str] = []
    out = asyncio.run(airports_router._build_and_store(
        airport, [], [], LAT, LON, R, body, "svg:k", "meta:k", warnings))

    assert out[0] == "<svg/>"
    assert "osm_layers_unavailable" in warnings
    assert stored == []


def test_degraded_render_is_not_stored_by_the_job_path(monkeypatch):
    from backend.airports.schemas import RenderRequest as ARequest
    from backend.jobs import _exec_airports as ex
    from backend.jobs import store as jobs_store
    import threading

    airport = {"ident": "LHBP", "name": "Budapest", "latitude_deg": LAT,
               "longitude_deg": LON}
    outcome = {"warning": "overture_unavailable", "stored": []}

    async def fake_lookup(icao):
        return airport, [], [], [], True

    async def fake_build(airport, runway_rows, freq_rows, lat, lon, radius_m,
                         body, warnings, **kwargs):
        warnings.append(outcome["warning"])
        return "<svg/>", {"runway": 1}, {"ways": 1}, 0.0, None, "ctx"

    async def record_store(*args, **kwargs):
        outcome["stored"].append(args)

    async def no_progress(*args, **kwargs):
        return None

    async def no_splits(*args, **kwargs):
        return None

    monkeypatch.setattr(ex, "_lookup", fake_lookup)
    monkeypatch.setattr(ex, "_render_uncached", fake_build)
    monkeypatch.setattr(ex, "_store_render", record_store)
    monkeypatch.setattr(ex, "_store_splits", no_splits)
    monkeypatch.setattr(jobs_store, "set_progress", no_progress)

    body = ARequest(icao="LHBP")
    stop = threading.Event()
    asyncio.run(ex._render_document(body, "job-1", stop, lambda: False,
                                    "/v1/airports/render"))
    assert outcome["stored"] == []

    outcome["warning"] = "overture_empty"  # complete Overpass data: cacheable
    asyncio.run(ex._render_document(body, "job-2", stop, lambda: False,
                                    "/v1/airports/render"))
    assert len(outcome["stored"]) == 1


def test_full_overpass_failure_maps_to_502_envelope(monkeypatch):
    _stub_load(monkeypatch, AirportOverpassError("all mirrors down"), [])

    result, err = asyncio.run(fetching._load_airfield(LAT, LON, R, []))

    assert result is None
    assert err is not None and err.status_code == 502
    body = json.loads(err.body)
    assert body["error"]["code"] == "overpass_unavailable"


def test_context_layers_stay_on_overpass(monkeypatch):
    queries: list[str] = []

    async def fake_fetch(query, client=None):
        queries.append(query)
        return []

    def no_airfield(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("context must not route through load_airfield")

    monkeypatch.setattr(fetching, "fetch_airport_polygons", fake_fetch)
    monkeypatch.setattr(fetching, "load_airfield", no_airfield)

    warnings: list[str] = []
    elements, err = asyncio.run(fetching._load_polygons(
        LAT, LON, R, warnings, kind="overpass-ctx"))

    assert err is None and elements == []
    assert queries == [build_context_query(LAT, LON, R)]


def test_raw_counts_report_kept_polylines_under_ways():
    assert airfield_raw_counts(_airfield()) == {
        "nodes": 0, "ways": 4, "relations": 0,
    }


def test_cached_airfield_renders_identical_svg(monkeypatch):
    """Preview and artwork share one render path: ``_render_uncached`` feeds
    ``_build_svg`` for the sync endpoints and the jobs alike, and the
    ``artwork: "inline"`` sheet path does not exist for airports. So the
    check here is that the cache round trip yields the same placement."""
    fresh = _airfield()
    cached = fetching._airfield_from_json(fetching._airfield_to_json(fresh))
    assert cached == fresh

    airport = {
        "ident": "LHBP", "name": "Budapest", "municipality": "Budapest",
        "iso_country": "HU", "latitude_deg": LAT, "longitude_deg": LON,
    }
    runway = {
        "le_ident": "13L", "he_ident": "31R", "length_ft": "3000",
        "width_ft": "150", "surface": "ASP",
        "le_latitude_deg": "47.4395", "le_longitude_deg": "19.2510",
        "he_latitude_deg": "47.4342", "he_longitude_deg": "19.2602",
    }

    def render(airfield):
        return _build_svg(
            airport, [runway], [], airfield, 1000, 5.0,
            taxiway_labels=True,
        )

    svg_fresh, *_ = render(fresh)
    svg_cached, *_ = render(cached)
    assert "<path" in svg_fresh
    assert svg_fresh == svg_cached


def test_airfield_layer_keeps_four_classes_only(monkeypatch):
    fresh = _airfield()
    fresh.geoms["stands"] = [[(19.2500, 47.4300), (19.2510, 47.4310)]]
    fresh.geoms["hangar"] = [[(19.2500, 47.4300), (19.2510, 47.4310)]]
    fresh.geoms["stopways"] = [[(19.2500, 47.4300), (19.2510, 47.4310)]]
    calls: list = []

    async def fake_load(lat, lon, radius_m, *, client=None):
        calls.append((lat, lon, radius_m))
        return fresh

    monkeypatch.setattr(bm, "load_airfield", fake_load)
    lines = asyncio.run(bm._fetch_layer("airfield", (47.0, 47.1, 19.0, 19.1), LAT, LON))

    assert calls == [(LAT, LON, bm.AIRFIELD_RADIUS_M)]
    assert len(lines) == 4  # runway, taxiway, apron, terminal
    assert all(len(ring) >= 2 for ring in lines)


def test_basemap_airfield_key_moved_to_osm3(monkeypatch):
    monkeypatch.setattr(settings, "overture_enabled", True)
    monkeypatch.setattr(settings, "overture_release", "2026-10-21.0")
    view = (47.0, 47.1, 19.0, 19.1)
    snapped = "%.3f,%.3f,%.3f,%.3f" % bm.quantise(view)
    assert bm.layer_key("airfield", view) == traffic_cache.traffic_cache_key(
        "osm3", "airfield", snapped, "overture=1", "release=2026-10-21.0",
        f"sources={bm.SOURCES_VERSION}")
    assert bm.layer_key("airfield", view) != traffic_cache.traffic_cache_key(
        "osm2", "airfield", snapped)


def test_basemap_airfield_key_follows_release_and_switch(monkeypatch):
    view = (47.0, 47.1, 19.0, 19.1)
    monkeypatch.setattr(settings, "overture_enabled", True)
    monkeypatch.setattr(settings, "overture_release", "2026-10-21.0")
    base = bm.layer_key("airfield", view)
    monkeypatch.setattr(settings, "overture_release", "2026-11-18.0")
    assert bm.layer_key("airfield", view) != base
    monkeypatch.setattr(settings, "overture_release", "2026-10-21.0")
    monkeypatch.setattr(settings, "overture_enabled", False)
    assert bm.layer_key("airfield", view) != base
    # The other layers do not read the airfield source: their key keeps the
    # plain view, whatever the Overture release says.
    monkeypatch.setattr(settings, "overture_release", "2026-11-18.0")
    snapped = "%.3f,%.3f,%.3f,%.3f" % bm.quantise(view)
    assert bm.layer_key("motorways", view) == traffic_cache.traffic_cache_key(
        "osm3", "motorways", snapped)


def test_degraded_airfield_layer_is_not_written_to_l3(monkeypatch):
    traffic_cache.configure_traffic_redis(None)
    degraded_view = (10.0, 10.1, 20.0, 20.1)
    complete_view = (30.0, 30.1, 40.0, 40.1)
    next_result: list[AirfieldResult] = []

    async def fake_load(lat, lon, radius_m, *, client=None):
        return next_result[0]

    monkeypatch.setattr(bm, "load_airfield", fake_load)

    next_result[:] = [_airfield(warnings=["overture_unavailable"])]
    base, failed = asyncio.run(bm.load_basemap(degraded_view, LAT, LON, ("airfield",)))
    assert failed == [] and base["airfield"]
    assert asyncio.run(traffic_cache.cache_get(bm.layer_key("airfield", degraded_view))) is None

    next_result[:] = [_airfield(warnings=["osm_layers_unavailable"])]
    asyncio.run(bm.load_basemap(degraded_view, LAT, LON, ("airfield",)))
    assert asyncio.run(traffic_cache.cache_get(bm.layer_key("airfield", degraded_view))) is None

    next_result[:] = [_airfield()]
    base, failed = asyncio.run(bm.load_basemap(complete_view, LAT, LON, ("airfield",)))
    assert failed == [] and base["airfield"]
    assert asyncio.run(traffic_cache.cache_get(bm.layer_key("airfield", complete_view))) is not None


def test_traffic_render_key_follows_overture_switch_release_and_source_version(monkeypatch):
    from backend.traffic import render_cache as traffic_render_cache
    from backend.traffic.schemas import RenderRequest as TRequest

    body = TRequest(icao="LHBP", date="2026-10-10")
    view = (47.0, 47.1, 19.0, 19.1)

    def svg_key() -> str:
        return traffic_render_cache._render_keys(
            "LHBP", "2026-10-10", body.t_from, body.t_to, view, body)[0]

    monkeypatch.setattr(settings, "overture_enabled", True)
    monkeypatch.setattr(settings, "overture_release", "2026-10-21.0")
    base = svg_key()

    monkeypatch.setattr(settings, "overture_enabled", False)
    assert svg_key() != base
    monkeypatch.setattr(settings, "overture_enabled", True)

    monkeypatch.setattr(settings, "overture_release", "2026-11-18.0")
    assert svg_key() != base
    monkeypatch.setattr(settings, "overture_release", "2026-10-21.0")

    monkeypatch.setattr(traffic_render_cache, "SOURCES_VERSION", "2")
    assert svg_key() != base
    monkeypatch.setattr(traffic_render_cache, "SOURCES_VERSION", "1")

    assert svg_key() == base


def test_degraded_airfield_is_reported_to_the_render_collector(monkeypatch):
    traffic_cache.configure_traffic_redis(None)
    view = (10.0, 10.1, 20.0, 20.1)
    next_result: list[AirfieldResult] = []

    async def fake_load(lat, lon, radius_m, *, client=None):
        return next_result[0]

    monkeypatch.setattr(bm, "load_airfield", fake_load)

    next_result[:] = [_airfield(warnings=["overture_unavailable"])]
    with bm.collect_uncached_layers() as uncached:
        base, failed = asyncio.run(bm.load_basemap(view, LAT, LON, ("airfield",)))
    assert failed == [] and base["airfield"]
    assert uncached == ["airfield"]

    next_result[:] = [_airfield()]
    with bm.collect_uncached_layers() as complete:
        base, failed = asyncio.run(bm.load_basemap(view, LAT, LON, ("airfield",)))
    assert failed == [] and base["airfield"]
    assert complete == []


def test_degraded_svg_is_served_by_key_but_is_no_render_hit():
    from backend.airports import render_cache
    from backend.airports.schemas import RenderRequest as ARequest

    body = ARequest(icao="LHBP")
    svg_key, meta_key = render_cache._render_keys("LHBP", 3500.0, body)
    asyncio.run(render_cache._store_degraded_svg(svg_key, "<svg/>"))

    assert asyncio.run(cache_get(svg_key)) == "<svg/>"
    warnings: list[str] = []
    assert asyncio.run(render_cache._load_cached_render(svg_key, meta_key, warnings)) is None
    assert "svg_cache_hit" not in warnings
