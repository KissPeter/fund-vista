"""Pydantic-owned query validation on ``GET /v1/traffic/availability``.

The endpoint used to hand-roll its checks (length of the ICAO, ISO date
parsing). The rules now live in pydantic models
(:mod:`backend.traffic.schemas`), the router only maps a
:class:`pydantic.ValidationError` onto the error envelope — so the wire
contract (status code + ``error.code``) must not drift.

Real HTTP against an in-process uvicorn server (no TestClient), like
``test_traffic_api.py``: the upstreams are stubbed, never called.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

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
            raise RuntimeError("traffic query test server never became healthy")
    with httpx.Client(base_url=base, timeout=60.0) as c:
        yield c
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _stub_upstreams(monkeypatch):
    """No upstream I/O: the airport lookup and the archive index are stubbed."""

    async def _fake_resolve(icao: str):
        return (dict(HU_ROW), False)

    async def _fake_source_status(day, client, today=None):
        return {"status": "available"}

    monkeypatch.setattr("backend.traffic.router.resolve_airport", _fake_resolve)
    monkeypatch.setattr("backend.traffic.availability.asource_status", _fake_source_status)


# -- the ICAO parameter ------------------------------------------------------

def test_icao_missing_or_empty_is_invalid_params(live):
    for params in ({}, {"icao": "", "date": "2026-10-10"},
                   {"icao": "   ", "date": "2026-10-10"}):
        resp = live.get("/v1/traffic/availability", params=params)
        assert resp.status_code == 422, (params, resp.text)
        assert resp.json()["error"]["code"] == "invalid_params"
        assert "icao must be 1-8 characters." in resp.json()["error"]["message"]


def test_icao_length_boundary_is_eight(live):
    # 8 characters: the format rule passes, so the request is judged by the
    # airport lookup, not by the format (the stubbed lookup answers).
    resp = live.get("/v1/traffic/availability",
                    params={"icao": "LHBPABCD", "date": "2026-10-10"})
    assert resp.status_code == 200, resp.text
    assert "error" not in resp.json()

    # 9 characters: one over the boundary -> format error.
    resp = live.get("/v1/traffic/availability",
                    params={"icao": "LHBPABCDE", "date": "2026-10-10"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "invalid_params"
    assert "icao must be 1-8 characters." in resp.json()["error"]["message"]


def test_icao_is_normalized(live):
    resp = live.get("/v1/traffic/availability",
                    params={"icao": " lhbp ", "date": "2026-10-10"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] in ("available", "ready", "out_of_range")


def test_icao_outside_hungary_is_unsupported_not_invalid(live):
    resp = live.get("/v1/traffic/availability",
                    params={"icao": "EGLL", "date": "2026-10-10"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "unsupported_airport"


# -- the date parameter ------------------------------------------------------

def test_date_must_be_iso(live):
    for date in ("10/10/2026", "2026-13-01", "2026-02-30", "yesterday", "",
                 "2026-10-10 ", " 2026-10-10"):
        resp = live.get("/v1/traffic/availability",
                        params={"icao": "LHBP", "date": date})
        assert resp.status_code == 422, (date, resp.text)
        assert resp.json()["error"]["code"] == "invalid_params"
        assert "date must be YYYY-MM-DD." in resp.json()["error"]["message"]


def test_date_is_checked_after_the_hungary_rule(live):
    # A non-HU ICAO wins over a broken date (the order the API always had).
    resp = live.get("/v1/traffic/availability",
                    params={"icao": "EGLL", "date": "not-a-date"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "unsupported_airport"


def test_valid_query_is_not_a_validation_error(live):
    resp = live.get("/v1/traffic/availability",
                    params={"icao": "LHBP", "date": "2026-10-10"})
    assert resp.status_code == 200, resp.text
    assert "error" not in resp.json()
