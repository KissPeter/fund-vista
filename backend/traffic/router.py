"""Traffic availability: ``GET /v1/traffic/availability?icao&date``.

Phase 1 read path (no archive touch): L1 slice hit → ``ready``; else one
GitHub API call → ``available`` / ``unavailable``; outside the 365-day
window → ``out_of_range``. ``queued`` / ``running`` are job-poll states
(``GET /v1/jobs/{id}.progress``), not answered here.

NAS-only until R2 (plan §6.5): ``/v1/traffic/*`` and traffic jobs have no
cloud fallback — nginx must exclude them (see ``docs/nas-fundvista.md``)
and the UI shows "traffic temporarily unavailable" instead of a confusing
``job_not_found`` from the cloud.
"""

from __future__ import annotations

import datetime as dt
import os

import httpx
from fastapi import APIRouter, Depends

from backend.airports.ourairports import AirportNotFoundError, resolve_airport
from backend.http import error_response
from backend.penplot.router import require_rate_limit
from backend.traffic import availability as _avail
from backend.traffic import store as _store

router = APIRouter(prefix="/v1/traffic", tags=["traffic-v1"])


@router.get("/availability", dependencies=[Depends(require_rate_limit)])
async def availability(icao: str = "", date: str = ""):
    """Cheap day check: ready (cached) | available | unavailable | out_of_range."""
    want = (icao or "").strip().upper()
    if not want or len(want) > 8:
        return error_response(422, "invalid_params", "icao must be 1-8 characters.")
    try:
        day = dt.date.fromisoformat(date)
    except ValueError:
        return error_response(422, "invalid_params", "date must be YYYY-MM-DD.")
    try:
        await resolve_airport(want)
    except AirportNotFoundError:
        return error_response(404, "airport_not_found", f"No airport found for ICAO '{want}'.")
    except Exception as exc:  # OurAirports upstream down → 502 like the airports tab
        return error_response(502, "ourairports_unavailable", f"Airport lookup failed: {exc}")
    key = _store.day_key(day.isoformat(), want)
    try:
        hit = _store.default_store().exists(key)
    except Exception:
        hit = False
    if hit:
        try:
            size = len(_store.default_store().get(key) or b"")
        except Exception:
            size = None
        body: dict = {"status": "ready"}
        if size is not None:
            body["bytes"] = size
        return body
    timeout = float(os.environ.get("TRAFFIC_GITHUB_TIMEOUT_S", "20"))
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            return await _avail.asource_status(day, client)
    except httpx.HTTPError as exc:
        return error_response(502, "traffic_source_unavailable", f"Archive index lookup failed: {exc}")
