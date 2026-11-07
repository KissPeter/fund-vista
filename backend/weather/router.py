"""Weather endpoints. Phase 1 serves availability only.

Render and import (``/v1/weather/render``, ``/v1/weather/import``) arrive in
Phase 2 and are deliberately absent here.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query

from backend.penplot.router import require_rate_limit
from backend.weather import availability as _avail
from backend.weather.schemas import AvailabilityResponse

router = APIRouter(prefix="/v1/weather", tags=["weather-v1"])


@router.get("/availability", response_model=AvailabilityResponse, dependencies=[Depends(require_rate_limit)])
async def availability(
    date: dt.date = Query(..., description="UTC analysis day, YYYY-MM-DD."),
    time: int = Query(12, ge=0, le=23, description="UTC analysis hour; offered: 0, 6, 12, 18."),
) -> AvailabilityResponse:
    return AvailabilityResponse(**_avail.check(date, time))
