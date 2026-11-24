"""Weather availability bounds: 1940 floor, today minus 6 days, 00/06/12/18 UTC."""

from __future__ import annotations

import datetime as dt

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.penplot.router import require_rate_limit
from backend.weather import availability as av
from backend.weather.router import router as weather_router

TODAY = dt.date(2026, 11, 7)


def test_latest_is_today_minus_six_days():
    assert av.latest_date(TODAY) == dt.date(2026, 11, 1)


def test_valid_day_and_hour_available():
    assert av.check(dt.date(2005, 5, 20), 12, TODAY) == {"status": "available"}
    assert av.check(dt.date(2026, 11, 1), 18, TODAY) == {"status": "available"}


def test_before_1940_rejected_with_floor():
    out = av.check(dt.date(1939, 12, 31), 12, TODAY)
    assert out == {"status": "out_of_range", "nearest": {"date": "1940-01-01", "time": 12}}


def test_newer_than_latency_rejected_with_latest():
    out = av.check(dt.date(2026, 11, 2), 12, TODAY)
    assert out["status"] == "out_of_range"
    assert out["nearest"] == {"date": "2026-11-01", "time": 12}


def test_hour_not_offered_moves_to_nearest_offered():
    assert av.nearest_hour(7) == 6
    assert av.nearest_hour(21) == 18
    assert av.nearest_hour(3) == 0  # tie goes earlier
    out = av.check(dt.date(2005, 5, 20), 7, TODAY)
    assert out == {"status": "out_of_range", "nearest": {"date": "2005-05-20", "time": 6}}


def test_endpoint_responses():
    app = FastAPI()
    app.include_router(weather_router)
    app.dependency_overrides[require_rate_limit] = lambda: None
    client = TestClient(app)
    ok = client.get("/v1/weather/availability", params={"date": "2005-05-20", "time": 12})
    assert ok.status_code == 200 and ok.json() == {"status": "available", "nearest": None}
    bad = client.get("/v1/weather/availability", params={"date": "1900-01-01", "time": 12})
    assert bad.status_code == 200
    assert bad.json()["status"] == "out_of_range"
    assert bad.json()["nearest"] == {"date": "1940-01-01", "time": 12}
