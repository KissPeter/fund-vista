"""Thin HTTP glue for /v1/airports. No diagram math lives here — only:

lookup (OurAirports) -> Redis cache -> Overpass -> geometry/render ->
schema out, with all failures mapped to the ``{"error": {"code",
"message"}}`` envelope (the globally registered penplot handlers already
cover ``PenPlotError`` and ``RequestValidationError``).

Expensive endpoints (render/import/results) share the penplot per-IP
rate-limit bucket; ``lookup`` is cheap (CSV cache hits) and unthrottled.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from backend.airports.cache import KEY_PREFIX, cache_get
from backend.airports.fetching import (
    _bucket_radius_m,
    _effective_radius_m,
    _load_polygons,
    _lookup,
    _runway_infos,
)
from backend.airports.ourairports import OurAirportsError, search_airports
from backend.airports.render import _build_svg, render_source_version
from backend.airports.render_cache import (
    _load_cached_render,
    _load_cached_splits,
    _render_keys,
    _split_keys,
    _store_render,
    _store_splits,
)
from backend.airports.schemas import (
    ImportResponse,
    LookupResponse,
    RenderRequest,
    RenderResponse,
    SearchResponse,
    normalize_icao,
)
from backend.cancel import (
    ClientCancelled,
    check_cancelled,
    log_and_499,
    start_disconnect_watcher,
)
from backend.http import error_response, fnum
from backend.penplot import imaging
from backend.penplot.errors import ErrorCode, PenPlotError
from backend.penplot.router import require_rate_limit, resolve_public_base
from backend.penplot.router import store as penplot_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/airports", tags=["airports-v1"])


def render_diagram_version() -> str:
    """Layout version baked into the SVG cache key (kills stale renders).

    Content hash of the renderer source — automatic on every change."""
    return render_source_version()


async def _render_uncached(
    airport: dict, runway_rows: list[dict], freq_rows: list[dict],
    lat: float, lon: float, radius_m: float,
    body: RenderRequest, warnings: list[str],
    request: Request | None = None,
    endpoint: str = "/v1/airports/render",
    started_mono: float = 0.0,
    stop: threading.Event | None = None,
) -> tuple[str, dict[str, int], dict[str, int], float,
           tuple[dict, dict] | None, dict | None, str, str] | JSONResponse:
    """Fetch polygons and render, off the event loop. Shared by render/import.

    P2: fetch raced (checkpoints 2/3); CPU build runs in a thread with
    ``stop`` polled every 2000 elements (checkpoint 4). P5: returns any
    split outputs that were computed (vs reused from cache) so the caller
    can persist them next to the render cache.
    """
    elements, err = await _load_polygons(
        lat, lon, radius_m, warnings,
        request=request, endpoint=endpoint, started_mono=started_mono,
    )
    if err is not None or elements is None:
        return err if err is not None else error_response(
            502, "overpass_unavailable", "Ground-layout fetch failed."
        )
    ctx_elements = None
    if body.needs_context():
        ctx_elements, _ = await _load_polygons(
            lat, lon, radius_m, warnings, kind="overpass-ctx",
            request=request, endpoint=endpoint, started_mono=started_mono)
        warnings.append("context_enabled")
    await check_cancelled(
        request, endpoint=endpoint, stage="svg_build", started_mono=started_mono)
    aer_key, ctx_key = _split_keys(lat, lon, radius_m)
    pre_geoms, pre_counts, pre_ctx = await _load_cached_splits(
        aer_key, ctx_key, ctx_elements is not None, warnings)
    cancelled_cb = stop.is_set if stop is not None else None
    try:
        svg_text, path_counts, raw_counts, rotation, render_warnings, \
            computed_aer, computed_ctx = (
                await asyncio.to_thread(
                    _build_svg, airport, runway_rows, freq_rows, elements,
                    body.width, body.min_path_len_m, ctx_elements, body.zoom,
                    body.effective_layers(), body.taxiway_labels,
                    cancelled_cb, pre_geoms, pre_counts, pre_ctx,
                )
            )
    except ClientCancelled as exc:
        raise log_and_499(
            endpoint=endpoint, stage=exc.stage or "svg_build",
            request=request, started_mono=started_mono,
        )
    warnings.extend(render_warnings)
    return (
        svg_text, path_counts, raw_counts, rotation,
        computed_aer, computed_ctx, aer_key, ctx_key,
    )


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str = Query(min_length=2, max_length=60),
    limit: int = Query(default=5, ge=1, le=10),
) -> SearchResponse | JSONResponse:
    """Freeform airport search over names, places, ICAO and IATA codes.

    Same-named places come back as a ranked list; feed the chosen
    candidate's ``icao`` to lookup/render/import.
    """
    try:
        candidates, hit = await search_airports(q, limit)
    except OurAirportsError as exc:
        return error_response(
            502, "ourairports_unavailable",
            f"Airport search failed: {exc.detail}",
        )
    return SearchResponse(
        query=q.strip(), candidates=candidates,  # type: ignore[arg-type]
        cache_hit=hit)


@router.get("/lookup", response_model=LookupResponse)
async def lookup(
    icao: str = Query(min_length=3, max_length=4),
) -> LookupResponse | JSONResponse:
    """Resolve an ICAO (or IATA) code to airport/runway/frequency metadata (no OSM)."""
    try:
        code = normalize_icao(icao)
    except ValueError as exc:
        return error_response(422, ErrorCode.INVALID_PARAMS, str(exc))
    resolved = await _lookup(code)
    if isinstance(resolved, JSONResponse):
        return resolved
    airport, runway_rows, freq_rows, warnings, cache_hit = resolved
    canonical = (airport.get("ident") or code).strip().upper()
    return LookupResponse(
        icao=canonical,
        name=airport.get("name", ""),
        municipality=airport.get("municipality", ""),
        iso_country=airport.get("iso_country", ""),
        latitude_deg=float(airport["latitude_deg"]),
        longitude_deg=float(airport["longitude_deg"]),
        elevation_ft=fnum(airport.get("elevation_ft")),
        iata=(airport.get("iata_code") or "").strip(),
        runways=_runway_infos(runway_rows),  # type: ignore[arg-type]
        frequencies=freq_rows,  # type: ignore[arg-type]
        warnings=warnings,
        cache_hit=cache_hit,
    )


async def _build_and_store(
    airport: dict, runway_rows: list[dict], freq_rows: list[dict],
    lat: float, lon: float, radius_m: float, body: RenderRequest,
    svg_key: str, meta_key: str, warnings: list[str],
    request: Request | None = None,
    endpoint: str = "/v1/airports/render",
    started_mono: float = 0.0,
) -> tuple[str, dict[str, int], dict[str, int], float] | JSONResponse:
    """Miss path shared by ``render`` and ``import_diagram``.

    Threaded build (P2 checkpoints inside ``_render_uncached``),
    499-guard, then split + render stores. Returns
    ``(svg_text, path_counts, raw_counts, rotation)`` or an error response.
    """
    await check_cancelled(request, endpoint=endpoint, stage="validation", started_mono=started_mono)
    stop = threading.Event()
    watch = start_disconnect_watcher(request, stop)
    watcher = asyncio.create_task(watch())
    try:
        built = await _render_uncached(
            airport, runway_rows, freq_rows, lat, lon, radius_m, body, warnings,
            request=request, endpoint=endpoint, started_mono=started_mono, stop=stop,
        )
    finally:
        stop.set()
        watcher.cancel()
    if isinstance(built, JSONResponse):
        return built
    svg_text, path_counts, raw_counts, rotation, \
        computed_aer, computed_ctx, aer_key, ctx_key = built
    await check_cancelled(request, endpoint=endpoint, stage="svg_build", started_mono=started_mono)
    await _store_splits(aer_key, ctx_key, computed_aer, computed_ctx)
    await _store_render(svg_key, meta_key, svg_text, path_counts,
                        raw_counts, rotation)
    return svg_text, path_counts, raw_counts, rotation


@router.post("/render", response_model=RenderResponse,
              dependencies=[Depends(require_rate_limit)])
async def render(body: RenderRequest, request: Request) -> RenderResponse | JSONResponse:
    """Fetch OSM ground polygons and render one blueprint SVG.

    P2 checkpoints (shapes unchanged): 1. after validation+cache; 2. before
    Overpass fetch (raced); 3. after fetch before build; 4. inside CPU loop
    (every 2000 elements). Abort → 499, no cache write.
    """
    endpoint = "/v1/airports/render"
    started = time.monotonic()
    resolved = await _lookup(body.icao)
    if isinstance(resolved, JSONResponse):
        return resolved
    airport, runway_rows, freq_rows, warnings, lookup_hit = resolved
    lat, lon = float(airport["latitude_deg"]), float(airport["longitude_deg"])
    icao = (airport.get("ident") or body.icao).strip().upper()
    radius_m = _bucket_radius_m(
        _effective_radius_m(airport, runway_rows, body.radius_m)
    )
    if radius_m > body.radius_m:
        warnings.append("radius_expanded_to_cover_runways")

    svg_key, meta_key = _render_keys(icao, radius_m, body)
    cached = await _load_cached_render(svg_key, meta_key, warnings)
    if cached is not None:
        _, path_counts, raw_counts, rotation = cached
        cache_hit = True
    else:
        cache_hit = False
        built = await _build_and_store(
            airport, runway_rows, freq_rows, lat, lon, radius_m, body,
            svg_key, meta_key, warnings,
            request=request, endpoint=endpoint, started_mono=started,
        )
        if isinstance(built, JSONResponse):
            return built
        _, path_counts, raw_counts, rotation = built

    base = resolve_public_base(request)
    token = svg_key.rsplit(":", 1)[-1]
    log.info(
        "airports.render icao=%r paths=%d cache_hit=%s",
        icao, sum(path_counts.values()), cache_hit,
    )
    return RenderResponse(
        icao=icao,
        name=airport.get("name", ""),
        municipality=airport.get("municipality", ""),
        iso_country=airport.get("iso_country", ""),
        runways=_runway_infos(runway_rows),  # type: ignore[arg-type]
        frequencies=freq_rows,  # type: ignore[arg-type]
        rotation_deg=rotation,
        path_counts=path_counts,
        raw_counts=raw_counts,
        svg_url=f"{base}/v1/airports/results/{token}",
        warnings=warnings,
        cache_hit=cache_hit,
    )


@router.post("/import", response_model=ImportResponse,
              dependencies=[Depends(require_rate_limit)])
async def import_diagram(body: RenderRequest, request: Request) -> ImportResponse | JSONResponse:
    """Render an airport diagram and register it as a penplot image.

    Returns ``image_id`` — convert it with ``POST /v1/convert`` exactly
    like an uploaded SVG (vector branch: every method applies — Contour
    traces directly, Centerline/Hatch/Flow rasterize first so threshold
    and tone shape them; pen/page/label/display all apply).

    Same P2 checkpoints as ``/render``.
    """
    endpoint = "/v1/airports/import"
    started = time.monotonic()
    resolved = await _lookup(body.icao)
    if isinstance(resolved, JSONResponse):
        return resolved
    airport, runway_rows, freq_rows, warnings, _ = resolved
    lat, lon = float(airport["latitude_deg"]), float(airport["longitude_deg"])
    icao = (airport.get("ident") or body.icao).strip().upper()
    radius_m = _bucket_radius_m(
        _effective_radius_m(airport, runway_rows, body.radius_m)
    )
    if radius_m > body.radius_m:
        warnings.append("radius_expanded_to_cover_runways")

    # The UI fires /render and then /import with the same payload on every
    # control change, so this is almost always the diagram just rendered.
    svg_key, meta_key = _render_keys(icao, radius_m, body)
    cached = await _load_cached_render(svg_key, meta_key, warnings)
    if cached is not None:
        svg_text, path_counts, raw_counts, rotation = cached
    else:
        built = await _build_and_store(
            airport, runway_rows, freq_rows, lat, lon, radius_m, body,
            svg_key, meta_key, warnings,
            request=request, endpoint=endpoint, started_mono=started,
        )
        if isinstance(built, JSONResponse):
            return built
        svg_text, path_counts, raw_counts, rotation = built

    if sum(path_counts.values()) == 0 and not runway_rows:
        return error_response(
            422, ErrorCode.INVALID_PARAMS,
            f"No diagram features found for '{icao}' — "
            "unknown field or empty OSM coverage.",
        )
    try:
        imaging.parse_svg_vectors(svg_text.encode("utf-8"))
    except PenPlotError as exc:
        return error_response(exc.status, exc.code, exc.message)
    image_id = penplot_store.put_image_bytes(svg_text.encode("utf-8"), "svg")
    log.info(
        "airports.import icao=%r paths=%d image=%s",
        icao, sum(path_counts.values()), image_id[:12],
    )
    return ImportResponse(
        image_id=image_id,
        icao=icao,
        name=airport.get("name", ""),
        municipality=airport.get("municipality", ""),
        iso_country=airport.get("iso_country", ""),
        rotation_deg=rotation,
        path_counts=path_counts,
        raw_counts=raw_counts,
        warnings=warnings,
    )


@router.get("/results/{token}", dependencies=[Depends(require_rate_limit)])
async def get_result(token: str) -> Response:
    """Serve a cached rendered SVG (sha1 token from ``svg_url``)."""
    if len(token) != 40 or any(c not in "0123456789abcdef" for c in token.lower()):
        return error_response(404, "result_not_found", "Unknown diagram result.")
    svg_text = await cache_get(f"{KEY_PREFIX}:svg:{token.lower()}")
    if svg_text is None:
        return error_response(
            404, "result_not_found",
            "Diagram result expired or unknown — re-run POST /v1/airports/render.",
        )
    return Response(content=svg_text, media_type="image/svg+xml")
