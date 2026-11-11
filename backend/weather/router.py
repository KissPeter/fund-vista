"""Weather endpoints: availability (Phase 1), render, import and results (Phase 2).

``/v1/weather/*`` is NAS-only like ``/v1/traffic/*``: nginx MUST exclude these
paths from the cloud fallback (see ``docs/nas-fundvista.md``). Sheets are never
composed here; the artwork goes to ``POST /v1/compose-sheet``.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from backend.http.errors import error_response
from backend.penplot import imaging
from backend.penplot.errors import PenPlotError
from backend.penplot.router import require_rate_limit, resolve_public_base
from backend.penplot.router import store as penplot_store
from backend.weather import availability as _avail
from backend.weather.config import WEATHER_RETAIN_HOURS
from backend.weather import service
from backend.weather.schemas import AvailabilityResponse, ImportResponse, RenderRequest, RenderResponse

router = APIRouter(prefix="/v1/weather", tags=["weather-v1"])

#: The shared ``{"error": {"code", "message"}}`` envelope every refusal uses
#: (:func:`backend.http.errors.error_response`), declared for the OpenAPI document.
_ERROR_SCHEMA = {
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {
            "type": "object",
            "required": ["code", "message"],
            "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
        }
    },
}


def _refusal(description: str) -> dict:
    """OpenAPI response entry for one refusal status: the error envelope."""
    return {"description": description, "content": {"application/json": {"schema": _ERROR_SCHEMA}}}


_RENDER_REFUSALS = {
    422: _refusal("out_of_range (date outside the window), weather_too_long, or invalid_params."),
    502: _refusal("weather_source_unavailable: the field store could not be read; retry later."),
}
_IMPORT_REFUSALS = {
    **_RENDER_REFUSALS,
    500: _refusal("weather_retention_failed: the design could not be kept for the order retention."),
}
_RESULT_REFUSALS = {
    404: _refusal("result_not_found: the render token is unknown or expired."),
}


@router.get("/availability", response_model=AvailabilityResponse, dependencies=[Depends(require_rate_limit)])
async def availability(
    date: dt.date = Query(..., description="UTC analysis day, YYYY-MM-DD."),
    time: int = Query(12, ge=0, le=23, description="UTC analysis hour; offered: 0, 6, 12, 18."),
) -> AvailabilityResponse:
    return AvailabilityResponse(**_avail.check(date, time))


async def _render(body: RenderRequest) -> tuple[service.RenderSpec, dict] | JSONResponse:
    spec = service.RenderSpec.build(
        date=body.date, time=body.time, region=body.region, orientation=body.orientation,
        layers=body.layers, cities=body.cities,
    )
    try:
        return spec, await service.render(spec)
    except service.WeatherError as exc:
        return error_response(exc.status, exc.code, exc.message)


@router.post("/render", response_model=RenderResponse, dependencies=[Depends(require_rate_limit)],
             responses=_RENDER_REFUSALS)
async def render(body: RenderRequest, request: Request) -> RenderResponse | JSONResponse:
    """Render one weather map (layered SVG, artwork only) and return its URL."""
    got = await _render(body)
    if isinstance(got, JSONResponse):
        return got
    spec, info = got
    base = resolve_public_base(request)
    return RenderResponse(
        date=body.date, time=body.time, region=body.region, orientation=body.orientation,
        layers=list(spec.layers), cities=list(spec.cities),
        svg_url=f"{base}/v1/weather/results/{info['token']}",
        stats=info["stats"], warnings=info["warnings"],
        attribution=info["attribution"], cache_hit=info["cache_hit"],
    )


@router.post("/import", response_model=ImportResponse, dependencies=[Depends(require_rate_limit)],
             responses=_IMPORT_REFUSALS)
async def import_artwork(body: RenderRequest) -> ImportResponse | JSONResponse:
    """Render the weather map and register it as a penplot image.

    Returns ``image_id``; convert it with ``POST /v1/convert`` like an uploaded SVG.
    """
    got = await _render(body)
    if isinstance(got, JSONResponse):
        return got
    spec, info = got
    data = info["svg"].encode("utf-8")
    try:
        imaging.parse_svg_vectors(data)
    except PenPlotError as exc:
        return error_response(exc.status, exc.code, exc.message)
    image_id = penplot_store.put_image_bytes(data, "svg")
    # Promote the copy to the order retention at import (never re-rendered later).
    # retain_image is idempotent and keeps the original marker time, so the check is
    # the store's configured retained TTL, not the time left on a repeat import.
    retained_ttl_s = penplot_store.retained_ttl.total_seconds()
    if retained_ttl_s < WEATHER_RETAIN_HOURS * 3600:
        return error_response(500, "weather_retention_failed",
                              "The image store's retained TTL is below the weather order "
                              f"retention ({WEATHER_RETAIN_HOURS} h); set retained_ttl_hours.")
    if penplot_store.retain_image(image_id) is None:
        return error_response(500, "weather_retention_failed",
                              "The weather design could not be kept for the order retention.")
    return ImportResponse(
        image_id=image_id, date=body.date, time=body.time, layers=list(spec.layers),
        stats=info["stats"], warnings=info["warnings"], attribution=info["attribution"],
    )


@router.get("/results/{token}", dependencies=[Depends(require_rate_limit)], responses=_RESULT_REFUSALS)
async def get_result(token: str) -> Response:
    """Serve a cached weather SVG (sha1 token from ``svg_url``)."""
    svg_text = await service.load_result_svg(token)
    if svg_text is None:
        return error_response(404, "result_not_found",
                              "Weather result expired or unknown - re-run POST /v1/weather/render.")
    return Response(content=svg_text, media_type="image/svg+xml")
