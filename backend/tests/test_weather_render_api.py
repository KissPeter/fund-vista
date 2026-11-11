"""Weather render, results and import endpoints. Hermetic: the field loader and
the geography are injected; no store, no network."""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.penplot.router import require_rate_limit
from backend.weather import service
from backend.weather.fields import load_subset as real_load_subset
from backend.weather.router import router as weather_router

from backend.tests.weather_render_fixtures import FakeReader, fixture_geo


@pytest.fixture
def client(monkeypatch):
    reader = FakeReader()

    async def fake_load_subset(when, region, _reader, fields):
        return await real_load_subset(when, region, reader, fields)

    monkeypatch.setattr(service, "load_subset", fake_load_subset)
    monkeypatch.setattr(service, "load_geography", lambda: fixture_geo())
    app = FastAPI()
    app.include_router(weather_router)
    app.dependency_overrides[require_rate_limit] = lambda: None
    return TestClient(app)


BODY = {"date": "2005-05-20", "time": 12}


def test_render_default_layers_returns_url_stats_and_attribution(client):
    r = client.post("/v1/weather/render", json=BODY)
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["layers"] == ["coast", "borders", "streamlines", "cities"]
    assert re.search(r"/v1/weather/results/[0-9a-f]{40}$", doc["svg_url"])
    assert "CC BY 4.0" in doc["attribution"]
    assert doc["stats"]["total"]["paths"] > 0
    assert doc["stats"]["total"]["minutes"] > 0


def test_results_endpoint_serves_the_same_artwork(client):
    doc = client.post("/v1/weather/render", json=BODY).json()
    token = doc["svg_url"].rsplit("/", 1)[-1]
    svg = client.get(f"/v1/weather/results/{token}")
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")
    assert "<text" not in svg.text and "<image" not in svg.text
    assert 'data-layer="streamlines"' in svg.text


def test_second_identical_render_is_a_cache_hit(client):
    body = {"date": "2005-05-21", "time": 6}
    first = client.post("/v1/weather/render", json=body).json()
    second = client.post("/v1/weather/render", json=body).json()
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert first["svg_url"] == second["svg_url"]


def test_out_of_range_date_is_422_with_nearest(client):
    r = client.post("/v1/weather/render", json={"date": "1939-12-31", "time": 12})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "out_of_range"


def test_unknown_layer_is_rejected(client):
    r = client.post("/v1/weather/render", json={**BODY, "layers": ["coast", "fronts"]})
    assert r.status_code == 422


def test_too_long_plot_is_refused_and_not_cached(client, monkeypatch):
    monkeypatch.setattr(service, "MAX_PLOT_LENGTH_M", 0.01)
    body = {"date": "2005-05-22", "time": 0, "layers": ["coast", "isobars", "isotherms"]}
    r = client.post("/v1/weather/render", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "weather_too_long"
    monkeypatch.setattr(service, "MAX_PLOT_LENGTH_M", 40.0)
    ok = client.post("/v1/weather/render", json=body)
    assert ok.status_code == 200
    assert ok.json()["cache_hit"] is False


def test_import_returns_an_image_id(client):
    r = client.post("/v1/weather/import", json={"date": "2005-05-23", "time": 18})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert re.fullmatch(r"[0-9a-f]{64}", doc["image_id"])
    assert "CC BY 4.0" in doc["attribution"]


def test_duplicate_and_oversized_layer_and_city_lists_are_422(client):
    from backend.weather.config import DEFAULT_CITIES

    dup_layers = client.post("/v1/weather/render", json={**BODY, "layers": ["coast", "coast"]})
    too_many_layers = client.post("/v1/weather/render",
                                  json={**BODY, "layers": ["coast"] * 9})
    dup_cities = client.post("/v1/weather/render",
                             json={**BODY, "cities": [DEFAULT_CITIES[0]] * 2})
    too_many_cities = client.post("/v1/weather/render",
                                  json={**BODY, "cities": [DEFAULT_CITIES[0]] * 13})
    assert dup_layers.status_code == 422, dup_layers.text
    assert too_many_layers.status_code == 422, too_many_layers.text
    assert dup_cities.status_code == 422, dup_cities.text
    assert too_many_cities.status_code == 422, too_many_cities.text


def test_source_failure_answers_generically_without_bucket_paths(client, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("cannot read gs://secret-bucket/era5/store.zarr")

    monkeypatch.setattr(service, "load_subset", boom)
    resp = client.post("/v1/weather/render", json={**BODY, "date": "2005-05-19"})
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "weather_source_unavailable"
    assert "gs://" not in resp.text
    assert "secret-bucket" not in resp.text


def test_repeat_import_of_the_same_design_is_200_with_the_same_image_id(client):
    first = client.post("/v1/weather/import", json=BODY)
    again = client.post("/v1/weather/import", json=BODY)
    assert first.status_code == 200, first.text
    assert again.status_code == 200, again.text
    assert again.json()["image_id"] == first.json()["image_id"]


@pytest.mark.parametrize("days", [2, 89])
def test_repeat_import_after_a_clock_advance_is_still_200(client, monkeypatch, days):
    import datetime as real_dt

    from backend.penplot import store as penplot_store_module

    first = client.post("/v1/weather/import", json=BODY)
    assert first.status_code == 200, first.text
    offset = real_dt.timedelta(days=days)

    class Clock(real_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return real_dt.datetime.now(tz) + offset

    monkeypatch.setattr(penplot_store_module, "datetime", Clock)
    again = client.post("/v1/weather/import", json=BODY)
    assert again.status_code == 200, again.text
    assert again.json()["image_id"] == first.json()["image_id"]


def test_store_with_a_retained_ttl_below_90_days_is_refused(client, monkeypatch):
    import datetime as real_dt

    from backend.penplot.router import store as penplot_store

    monkeypatch.setattr(penplot_store, "retained_ttl", real_dt.timedelta(days=30))
    resp = client.post("/v1/weather/import", json=BODY)
    assert resp.status_code == 500, resp.text
    assert resp.json()["error"]["code"] == "weather_retention_failed"

