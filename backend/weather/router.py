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
from backend.weather import service
from backend.weather.schemas import AvailabilityResponse, ImportResponse, RenderRequest, RenderResponse

router = APIRouter(prefix="/v1/weather", tags=["weather-v1"])


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


@router.post("/render", response_model=RenderResponse, dependencies=[Depends(require_rate_limit)])
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


@router.post("/import", response_model=ImportResponse, dependencies=[Depends(require_rate_limit)])
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
    return ImportResponse(
        image_id=image_id, date=body.date, time=body.time, layers=list(spec.layers),
        stats=info["stats"], warnings=info["warnings"], attribution=info["attribution"],
    )


@router.get("/results/{token}", dependencies=[Depends(require_rate_limit)])
async def get_result(token: str) -> Response:
    """Serve a cached weather SVG (sha1 token from ``svg_url``)."""
    svg_text = await service.load_result_svg(token)
    if svg_text is None:
        return error_response(404, "result_not_found",
                              "Weather result expired or unknown - re-run POST /v1/weather/render.")
    return Response(content=svg_text, media_type="image/svg+xml")
