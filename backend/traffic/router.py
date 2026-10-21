"""Traffic endpoints (Phase 1: availability; Phase 2: render + import;
Phase 5: extra filters + category pens + window stats).

Render never touches the archive: it needs the cached L1 day dataset and
the L3 basemap. If L1 is missing it answers ``409 traffic_not_ingested``
and the UI offers "Load this day". Mac-only until R2 (plan §6.5):
``/v1/traffic/*`` has no cloud fallback — nginx must exclude these paths
(see ``docs/nas-fundvista.md``).
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import os
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from backend.airports.ourairports import AirportNotFoundError, resolve_airport
from backend.cancel import check_cancelled, race_cancel
from backend.http import tls
from backend.http.errors import error_response
from backend.penplot import imaging
from backend.penplot.errors import PenPlotError
from backend.penplot.router import require_rate_limit, resolve_public_base
from backend.penplot.router import store as penplot_store
from backend.traffic import availability as _avail
from backend.traffic import cache as _cache
from backend.traffic import store as _store
from backend.traffic.basemap import load_basemap
from backend.traffic.classify import classify_day
from backend.traffic.hungary import MESSAGE as _HU_MESSAGE
from backend.traffic.hungary import is_hungarian_icao, row_is_hungarian
from backend.traffic.render import render_svg
from backend.traffic.render_cache import (
    _load_cached_render,
    _load_result_svg,
    _render_keys,
    _store_render,
    _svg_token,
)
from backend.traffic.schemas import (
    ATTRIBUTION,
    ImportResponse,
    RenderRequest,
    RenderResponse,
    movements_key,
)
from backend.traffic.stats import movement_stats

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/traffic", tags=["traffic-v1"])


@router.get("/availability", dependencies=[Depends(require_rate_limit)])
async def availability(icao: str = "", date: str = ""):
    """Cheap day check: ready (cached) | available | unavailable | out_of_range."""
    want = (icao or "").strip().upper()
    if not want or len(want) > 8:
        return error_response(422, "invalid_params", "icao must be 1-8 characters.")
    if not is_hungarian_icao(want):
        return error_response(422, "unsupported_airport", _HU_MESSAGE)
    try:
        day = dt.date.fromisoformat(date)
    except ValueError:
        return error_response(422, "invalid_params", "date must be YYYY-MM-DD.")
    try:
        airport_row, _hit = await resolve_airport(want)
    except AirportNotFoundError:
        return error_response(404, "airport_not_found", f"No airport found for ICAO '{want}'.")
    except Exception as exc:  # OurAirports upstream down → 502 like the airports tab
        return error_response(502, "ourairports_unavailable", f"Airport lookup failed: {exc}")
    if not row_is_hungarian(airport_row):
        return error_response(422, "unsupported_airport", _HU_MESSAGE)
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
        async with tls.async_client(timeout=httpx.Timeout(timeout)) as client:
            return await _avail.asource_status(day, client)
    except httpx.HTTPError as exc:
        return error_response(502, "traffic_source_unavailable", f"Archive index lookup failed: {exc}")


def _fnum(v: object) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def _render_document(
    body: RenderRequest, request: Request, endpoint: str, started_mono: float,
) -> dict | JSONResponse:
    """Shared validation → L1 → L2/L3 → render + L4 store for render/import.

    Returns ``{"airport", "icao", "counts", "movements", "stats",
    "svg_text", "warnings", "cache_hit", "svg_key"}`` or an error response.
    Never touches the archive: a missing L1 answers ``409
    traffic_not_ingested``. A dead basemap degrades to empty layers +
    ``basemap_unavailable`` warning, never a failed render.
    """
    day = dt.date.fromisoformat(body.date)
    lo, hi = _avail.selectable_range()
    if not lo <= day <= hi:
        return error_response(422, "out_of_range",
                              f"Date {body.date} outside {lo.isoformat()}..{hi.isoformat()}.")
    try:
        airport, _hit = await resolve_airport(body.icao)
    except AirportNotFoundError:
        return error_response(404, "airport_not_found", f"No airport found for ICAO '{body.icao}'.")
    except Exception as exc:
        return error_response(502, "ourairports_unavailable", f"Airport lookup failed: {exc}")
    if not row_is_hungarian(airport):
        return error_response(422, "unsupported_airport", _HU_MESSAGE)
    lat = _fnum(airport.get("latitude_deg"))
    lon = _fnum(airport.get("longitude_deg"))
    if lat is None or lon is None:
        return error_response(502, "ourairports_unavailable",
                              f"Airport '{body.icao}' has no coordinates.")
    icao = (airport.get("ident") or body.icao).strip().upper()

    await check_cancelled(request, endpoint=endpoint, stage="validation", started_mono=started_mono)
    try:
        raw = _store.default_store().get(_store.day_key(day.isoformat(), icao))
    except Exception:
        raw = None
    if raw is None:
        return error_response(409, "traffic_not_ingested",
                              f"No loaded traffic for {icao} on {body.date} — "
                              "load the day first (POST /v1/jobs type traffic_ingest).")
    try:
        data = json.loads(gzip.decompress(raw).decode("utf-8"))
    except (ValueError, OSError):
        return error_response(502, "traffic_store_unavailable",
                              f"Stored traffic for {icao} on {body.date} is unreadable.")
    l1_bbox = data.get("bbox") or []
    if len(l1_bbox) != 4:
        return error_response(502, "traffic_store_unavailable",
                              f"Stored traffic for {icao} on {body.date} has no view bbox.")

    t_from_ts, t_to_ts = body.day_range(day)
    if body.view_south is None:
        view = (float(l1_bbox[0]), float(l1_bbox[1]), float(l1_bbox[2]), float(l1_bbox[3]))
    else:
        view = (body.view_south, body.view_north, body.view_west, body.view_east)  # type: ignore[misc]
        if not (view[0] >= l1_bbox[0] and view[1] <= l1_bbox[1]
                and view[2] >= l1_bbox[2] and view[3] <= l1_bbox[3]):
            return error_response(422, "invalid_params",
                                  "view must lie inside the loaded day bbox "
                                  f"({l1_bbox[0]:.2f},{l1_bbox[1]:.2f},{l1_bbox[2]:.2f},{l1_bbox[3]:.2f}).")

    svg_key, meta_key = _render_keys(icao, day.isoformat(), body.t_from, body.t_to, view, body)
    cached = await _load_cached_render(svg_key, meta_key, warnings := [])
    if cached is not None:
        svg_text, counts, movements, stats = cached
        return {"airport": airport, "icao": icao, "counts": counts,
                "movements": movements, "stats": stats, "svg_text": svg_text,
                "warnings": warnings, "cache_hit": True, "svg_key": svg_key}

    await check_cancelled(request, endpoint=endpoint, stage="overpass", started_mono=started_mono)
    l2_key = _cache.traffic_cache_key("mov", movements_key(icao, day.isoformat()))
    moves_json = await _cache.cache_get(l2_key)
    if moves_json is not None:
        try:
            moves = json.loads(moves_json)
        except ValueError:
            moves = None
    else:
        moves = None
    if moves is None:
        moves = classify_day(data)
        try:
            await _cache.cache_set(l2_key, json.dumps(moves, separators=(",", ":")))
        except Exception:
            pass
    window = [m for m in moves if t_from_ts <= m["t"] < t_to_ts]

    try:
        base, failed = await race_cancel(
            request, load_basemap(view, lat, lon),
            endpoint=endpoint, stage="overpass", started_mono=started_mono,
        )
    except HTTPException:
        raise  # cancel → 499 must propagate, never degrade into a response
    except Exception:
        # load_basemap already degrades per layer; this is belt-and-braces
        # for anything unexpected from the fetch layer.
        base, failed = {k: [] for k in ("motorways", "waterways", "water", "airfield")}, ["all"]
    await check_cancelled(request, endpoint=endpoint, stage="svg_build", started_mono=started_mono)
    flt = body.filters(t_from_ts, t_to_ts)
    try:
        elev_ft = float(data["airport"].get("elev_ft", 0.0) or 0.0)
    except (TypeError, ValueError):
        elev_ft = 0.0
    svg_text, counts = render_svg(window, base, view, flt,
                                  {"lat": lat, "lon": lon, "elev_ft": elev_ft},
                                  width=float(body.width))
    stats = movement_stats(window)
    if failed:
        warnings.append("basemap_unavailable")
    await _store_render(svg_key, meta_key, svg_text, counts, len(window), stats)
    log.info("traffic.render icao=%s day=%s moves=%d cache_hit=False", icao, body.date, len(window))
    return {"airport": airport, "icao": icao, "counts": counts,
            "movements": len(window), "stats": stats, "svg_text": svg_text,
            "warnings": warnings, "cache_hit": False, "svg_key": svg_key}


@router.post("/render", response_model=RenderResponse,
             dependencies=[Depends(require_rate_limit)])
async def render(body: RenderRequest, request: Request) -> RenderResponse | JSONResponse:
    """Render one traffic map SVG (L2 movements + L3 basemap, L4-cached).

    P2 checkpoints: after validation+L1, around the Overpass fetch (raced),
    before the build. Abort → 499, no cache write.
    """
    endpoint = "/v1/traffic/render"
    started = time.monotonic()
    info = await _render_document(body, request, endpoint, started)
    if isinstance(info, JSONResponse):
        return info
    base = resolve_public_base(request)
    token = _svg_token(info["svg_key"])
    return RenderResponse(
        icao=info["icao"], date=body.date, t_from=body.t_from, t_to=body.t_to,
        counts=info["counts"], movements=info["movements"],
        stats=info["stats"],
        svg_url=f"{base}/v1/traffic/results/{token}",
        attribution=ATTRIBUTION,
        warnings=info["warnings"], cache_hit=info["cache_hit"],
    )


@router.post("/import", response_model=ImportResponse,
             dependencies=[Depends(require_rate_limit)])
async def import_diagram(body: RenderRequest, request: Request) -> ImportResponse | JSONResponse:
    """Render a traffic map and register it as a penplot image.

    Returns ``image_id`` — convert it with ``POST /v1/convert`` exactly
    like an uploaded SVG (vector branch). An empty movement selection still
    imports (the basemap plots); counts tell the story.
    """
    endpoint = "/v1/traffic/import"
    started = time.monotonic()
    info = await _render_document(body, request, endpoint, started)
    if isinstance(info, JSONResponse):
        return info
    try:
        imaging.parse_svg_vectors(info["svg_text"].encode("utf-8"))
    except PenPlotError as exc:
        return error_response(exc.status, exc.code, exc.message)
    image_id = penplot_store.put_image_bytes(info["svg_text"].encode("utf-8"), "svg")
    log.info("traffic.import icao=%s moves=%d image=%s",
             info["icao"], info["movements"], image_id[:12])
    return ImportResponse(
        image_id=image_id,
        icao=info["icao"], date=body.date, t_from=body.t_from, t_to=body.t_to,
        counts=info["counts"], movements=info["movements"],
        stats=info["stats"],
        attribution=ATTRIBUTION, warnings=info["warnings"],
    )


@router.get("/results/{token}", dependencies=[Depends(require_rate_limit)])
async def get_result(token: str) -> Response:
    """Serve a cached rendered traffic SVG (sha1 token from ``svg_url``)."""
    svg_text = await _load_result_svg(token)
    if svg_text is None:
        return error_response(404, "result_not_found",
                              "Traffic result expired or unknown — "
                              "re-run POST /v1/traffic/render.")
    return Response(content=svg_text, media_type="image/svg+xml")
