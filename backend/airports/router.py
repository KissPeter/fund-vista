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
import json
import logging

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from backend.airports import cache as cache_mod
from backend.airports.cache import airports_cache_key, cache_get, cache_set
from backend.airports.config import settings
from backend.airports.ourairports import (
    AirportNotFoundError,
    OurAirportsError,
    airport_frequencies,
    airport_runways,
    resolve_airport,
    search_airports,
)
from backend.airports.overpass import (
    AirportOverpassError,
    build_airport_query,
    fetch_airport_polygons,
    split_aeroway,
)
from backend.airports.render import render_diagram
from backend.airports.schemas import (
    ImportResponse,
    LookupResponse,
    RenderRequest,
    RenderResponse,
    SearchResponse,
)
from backend.penplot import imaging
from backend.penplot.errors import ErrorCode, PenPlotError
from backend.penplot.router import require_rate_limit
from backend.penplot.router import store as penplot_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/airports", tags=["airports-v1"])


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}
    )


def _fnum(value: object) -> float | None:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


async def _lookup(icao: str) -> tuple[dict, list[dict], list[dict], list[str], bool] | JSONResponse:
    """Resolve airport + runways + frequencies (cached CSVs).

    Returns ``(airport, runway_rows, frequencies, warnings, cache_hit)`` or
    an error response (unknown ICAO, upstream down).
    """
    warnings: list[str] = []
    try:
        airport, hit_airport = await resolve_airport(icao)
        runway_rows, hit_rwy = await airport_runways(airport["ident"])
        freq_rows, hit_freq = await airport_frequencies(airport["ident"])
    except AirportNotFoundError:
        return _error(404, "airport_not_found", f"No airport found for ICAO '{icao}'.")
    except OurAirportsError as exc:
        return _error(502, "ourairports_unavailable", f"Airport data fetch failed: {exc.detail}")
    cache_hit = bool(hit_airport and hit_rwy and hit_freq)
    if cache_hit:
        warnings.append("ourairports_cache_hit")
    return airport, runway_rows, freq_rows, warnings, cache_hit


def _runway_infos(runway_rows: list[dict]) -> list[dict]:
    from backend.airports.ourairports import heading_from_ident

    infos = []
    for row in runway_rows:
        le_ident = (row.get("le_ident") or "").strip()
        he_ident = (row.get("he_ident") or "").strip()
        infos.append(
            {
                "le_ident": le_ident,
                "he_ident": he_ident,
                "length_ft": _fnum(row.get("length_ft")),
                "width_ft": _fnum(row.get("width_ft")),
                "surface": (row.get("surface") or "").strip(),
                "le_heading_deg": _fnum(row.get("le_heading_degT"))
                or heading_from_ident(le_ident),
                "he_heading_deg": _fnum(row.get("he_heading_degT"))
                or heading_from_ident(he_ident),
                "endpoints_derived": False,
            }
        )
    return infos


async def _load_polygons(
    lat: float, lon: float, radius_m: float, warnings: list[str],
    kind: str = "overpass",
) -> tuple[list[dict] | None, JSONResponse | None]:
    """Redis-first Overpass ``around`` fetch. Returns (elements, None) or
    (None, error response) when every mirror fails. ``kind`` namespaces the
    cache key (``overpass`` vs ``overpass-ctx``). Context outages degrade to
    an empty layer (warning) instead of failing the whole render."""
    from backend.airports.overpass import build_context_query

    key = cache_mod.airports_cache_key(
        kind, f"{lat:.5f},{lon:.5f}", f"r={radius_m:.0f}"
    )
    raw_json = await cache_get(key)
    if raw_json is not None:
        warnings.append("overpass_cache_hit")
        try:
            return json.loads(raw_json), None
        except ValueError:
            pass
    query = (
        build_airport_query(lat, lon, radius_m)
        if kind == "overpass"
        else build_context_query(lat, lon, radius_m)
    )
    try:
        elements = await fetch_airport_polygons(query)
    except AirportOverpassError as exc:
        if kind != "overpass":
            warnings.append("context_unavailable")
            return [], None
        return None, _error(
            502,
            "overpass_unavailable",
            f"Ground-layout fetch failed ({exc.detail}). "
            "(Upstreams busy — retry in a minute.)",
        )
    await cache_set(key, json.dumps(elements))
    return elements, None


def _build_svg(
    airport: dict,
    runway_rows: list[dict],
    freq_rows: list[dict],
    elements: list[dict],
    width: int,
    min_path_len_m: float,
    context_elements: list[dict] | None = None,
    zoom: float = 1.0,
) -> tuple[str, dict[str, int], dict[str, int], float, list[str]]:
    from backend.airports.overpass import split_context

    geoms, raw_counts = split_aeroway(elements)
    ctx_geoms = split_context(context_elements or []) if context_elements else {}
    if context_elements:
        raw_counts = {**raw_counts, "context_ways": sum(len(v) for v in ctx_geoms.values())}
    svg, path_counts, rotation, warnings = render_diagram(
        airport=airport,
        runways=runway_rows,
        frequencies=[
            {
                "type": r["type"],
                "description": r["description"],
                "frequency_mhz": r["frequency_mhz"],
            }
            for r in freq_rows
        ],
        osm_geoms=geoms,
        context_geoms=ctx_geoms,
        width=width,
        min_path_len_m=min_path_len_m,
        zoom=zoom,
    )
    return svg, path_counts, raw_counts, rotation, warnings


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
        return _error(
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
    from backend.airports.schemas import normalize_icao

    try:
        code = normalize_icao(icao)
    except ValueError as exc:
        return _error(422, ErrorCode.INVALID_PARAMS, str(exc))
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
        elevation_ft=_fnum(airport.get("elevation_ft")),
        iata=(airport.get("iata_code") or "").strip(),
        runways=_runway_infos(runway_rows),  # type: ignore[arg-type]
        frequencies=freq_rows,  # type: ignore[arg-type]
        warnings=warnings,
        cache_hit=cache_hit,
    )


@router.post("/render", response_model=RenderResponse,
             dependencies=[Depends(require_rate_limit)])
async def render(body: RenderRequest, request: Request) -> RenderResponse | JSONResponse:
    """Fetch OSM ground polygons and render one blueprint SVG."""
    resolved = await _lookup(body.icao)
    if isinstance(resolved, JSONResponse):
        return resolved
    airport, runway_rows, freq_rows, warnings, lookup_hit = resolved
    lat, lon = float(airport["latitude_deg"]), float(airport["longitude_deg"])
    icao = (airport.get("ident") or body.icao).strip().upper()

    svg_key = airports_cache_key(
        "svg", icao, f"r={body.radius_m:.0f}",
        f"minlen={body.min_path_len_m}", f"width={body.width}",
        f"ctx={body.context}", f"zoom={body.zoom}",
    )
    cached_svg = await cache_get(svg_key)
    if cached_svg is not None:
        svg_text = cached_svg
        cache_hit = True
        warnings.append("svg_cache_hit")
        elements, err = await _load_polygons(lat, lon, body.radius_m, warnings)
        if err is not None or elements is None:
            return err  # type: ignore[return-value]
        ctx_elements: list[dict] | None = None
        if body.context:
            ctx_elements, _ = await _load_polygons(
                lat, lon, body.radius_m, warnings, kind="overpass-ctx")
            warnings.append("context_enabled")
        geoms, raw_counts = await asyncio.to_thread(split_aeroway, elements)
        path_counts = {cls: len(geoms.get(cls, [])) for cls in geoms}
        render_kwargs: dict = {
            "airport": airport,
            "runways": runway_rows,
            "frequencies": [
                {"type": r["type"], "description": r["description"],
                 "frequency_mhz": r["frequency_mhz"]} for r in freq_rows
            ],
            "osm_geoms": geoms,
            "width": body.width,
            "min_path_len_m": body.min_path_len_m,
            "zoom": body.zoom,
        }
        if ctx_elements:
            from backend.airports.overpass import split_context

            ctx_geoms = await asyncio.to_thread(split_context, ctx_elements)
            render_kwargs["context_geoms"] = ctx_geoms
            path_counts["context"] = sum(len(v) for v in ctx_geoms.values())
        else:
            path_counts["context"] = 0
        _, _, rotation, _ = await asyncio.to_thread(
            render_diagram, **render_kwargs)
    else:
        cache_hit = False
        elements, err = await _load_polygons(lat, lon, body.radius_m, warnings)
        if err is not None or elements is None:
            return err  # type: ignore[return-value]
        ctx_elements = None
        if body.context:
            ctx_elements, _ = await _load_polygons(
                lat, lon, body.radius_m, warnings, kind="overpass-ctx")
            warnings.append("context_enabled")
        svg_text, path_counts, raw_counts, rotation, render_warnings = (
            await asyncio.to_thread(
                _build_svg, airport, runway_rows, freq_rows, elements,
                body.width, body.min_path_len_m, ctx_elements, body.zoom,
            )
        )
        warnings.extend(render_warnings)
        await cache_set(svg_key, svg_text)

    base = str(request.base_url).rstrip("/")
    token = svg_key.rsplit(":", 1)[-1]
    log.info(
        "airports.render icao=%r paths=%d cache_hit=%s",
        icao, sum(path_counts.values()), cache_hit,
    )
    return RenderResponse(
        icao=icao,
        name=airport.get("name", ""),
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
async def import_diagram(body: RenderRequest) -> ImportResponse | JSONResponse:
    """Render an airport diagram and register it as a penplot image.

    Returns ``image_id`` — convert it with ``POST /v1/convert`` exactly
    like an uploaded SVG (vector branch: shading sliders are ignored,
    pen/page/label/display all apply).
    """
    resolved = await _lookup(body.icao)
    if isinstance(resolved, JSONResponse):
        return resolved
    airport, runway_rows, freq_rows, warnings, _ = resolved
    lat, lon = float(airport["latitude_deg"]), float(airport["longitude_deg"])
    icao = (airport.get("ident") or body.icao).strip().upper()
    elements, err = await _load_polygons(lat, lon, body.radius_m, warnings)
    if err is not None or elements is None:
        return err  # type: ignore[return-value]
    ctx_elements = None
    if body.context:
        ctx_elements, _ = await _load_polygons(
            lat, lon, body.radius_m, warnings, kind="overpass-ctx")
        warnings.append("context_enabled")
    svg_text, path_counts, raw_counts, rotation, render_warnings = (
        await asyncio.to_thread(
            _build_svg, airport, runway_rows, freq_rows, elements,
            body.width, body.min_path_len_m, ctx_elements, body.zoom,
        )
    )
    warnings.extend(render_warnings)
    if sum(path_counts.values()) == 0 and not runway_rows:
        return _error(
            422, ErrorCode.INVALID_PARAMS,
            f"No diagram features found for '{icao}' — "
            "unknown field or empty OSM coverage.",
        )
    try:
        imaging.parse_svg_vectors(svg_text.encode("utf-8"))
    except PenPlotError as exc:
        return _error(exc.status, exc.code, exc.message)
    image_id = penplot_store.put_image_bytes(svg_text.encode("utf-8"), "svg")
    log.info(
        "airports.import icao=%r paths=%d image=%s",
        icao, sum(path_counts.values()), image_id[:12],
    )
    return ImportResponse(
        image_id=image_id,
        icao=icao,
        name=airport.get("name", ""),
        rotation_deg=rotation,
        path_counts=path_counts,
        raw_counts=raw_counts,
        warnings=warnings,
    )


@router.get("/results/{token}", dependencies=[Depends(require_rate_limit)])
async def get_result(token: str) -> Response:
    """Serve a cached rendered SVG (sha1 token from ``svg_url``)."""
    if len(token) != 40 or any(c not in "0123456789abcdef" for c in token.lower()):
        return _error(404, "result_not_found", "Unknown diagram result.")
    svg_text = await cache_get(f"fund-vista:airports:v1:svg:{token.lower()}")
    if svg_text is None:
        return _error(
            404, "result_not_found",
            "Diagram result expired or unknown — re-run POST /v1/airports/render.",
        )
    return Response(content=svg_text, media_type="image/svg+xml")
