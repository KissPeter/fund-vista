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
import math
import threading
import time

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from backend.cancel import (
    ClientCancelled,
    check_cancelled,
    log_and_499,
    race_cancel,
    start_disconnect_watcher,
)

from backend.airports import cache as cache_mod
from backend.airports.cache import (
    KEY_PREFIX,
    airports_cache_key,
    cache_get,
    cache_get_many,
    cache_set,
    cache_set_many,
)
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
from backend.penplot.router import resolve_public_base
from backend.penplot.router import store as penplot_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/airports", tags=["airports-v1"])


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}
    )


def render_diagram_version() -> str:
    """Layout version baked into the SVG cache key (kills stale renders).

    Content hash of the renderer source — automatic on every change."""
    from backend.airports.render import render_source_version

    return render_source_version()


# P5: split outputs are cached independently of the render params (width/
# minlen/zoom/layers/labels all render from the same aeroway/context rings).
# Keys mirror the Overpass payload key (lat/lon/radius) so a slider change
# with the same airport reuses the split and skips the per-render CPU. Bump
# the version when split_aeroway/split_context semantics change.
SPLIT_VERSION = "split-v1"


def _split_keys(lat: float, lon: float, radius_m: float) -> tuple[str, str]:
    center = f"{lat:.5f},{lon:.5f}"
    radius = f"r={radius_m:.0f}"
    return (
        airports_cache_key("split-aeroway", center, radius, SPLIT_VERSION),
        airports_cache_key("split-ctx", center, radius, SPLIT_VERSION),
    )


def _unjson_geoms(geoms: dict, classes: list[str]) -> dict[str, list[list[tuple[float, float]]]]:
    return {
        cls: [[tuple(pt) for pt in pl] for pl in geoms[cls]]
        for cls in classes
    }


async def _load_cached_splits(
    aer_key: str, ctx_key: str, wants_context: bool, warnings: list[str],
) -> tuple[dict | None, dict | None, dict | None]:
    """Cached ``(aeroway geoms, raw_counts, context geoms)`` or None entries."""
    from backend.airports.overpass import AEROWAY_CLASSES, CONTEXT_CLASSES

    keys = [aer_key]
    if wants_context:
        keys.append(ctx_key)
    found = await cache_get_many(keys)
    aer_geoms = raw_counts = ctx_geoms = None
    aer_json = found.get(aer_key)
    if aer_json is not None:
        try:
            data = json.loads(aer_json)
            aer_geoms = _unjson_geoms(data["geoms"], AEROWAY_CLASSES)
            raw_counts = data["raw_counts"]
        except (ValueError, KeyError, TypeError):
            aer_geoms = raw_counts = None
    if aer_geoms is not None:
        warnings.append("aeroway_split_cache_hit")
    if wants_context:
        ctx_json = found.get(ctx_key)
        if ctx_json is not None:
            try:
                ctx_geoms = _unjson_geoms(json.loads(ctx_json)["geoms"], CONTEXT_CLASSES)
            except (ValueError, KeyError, TypeError):
                ctx_geoms = None
        if ctx_geoms is not None:
            warnings.append("context_split_cache_hit")
    return aer_geoms, raw_counts, ctx_geoms


async def _store_splits(
    aer_key: str, ctx_key: str,
    aer: tuple[dict[str, list[list[tuple[float, float]]]], dict[str, int]] | None,
    ctx: dict[str, list[list[tuple[float, float]]]] | None,
) -> None:
    items: dict[str, str] = {}
    if aer is not None:
        geoms, raw_counts = aer
        items[aer_key] = json.dumps(
            {"geoms": geoms, "raw_counts": raw_counts}, separators=(",", ":")
        )
    if ctx is not None:
        items[ctx_key] = json.dumps({"geoms": ctx}, separators=(",", ":"))
    if items:
        await cache_set_many(items)


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
    request: Request | None = None,
    endpoint: str = "/v1/airports/render",
    started_mono: float = 0.0,
) -> tuple[list[dict] | None, JSONResponse | None]:
    """Redis-first Overpass ``around`` fetch. Returns (elements, None) or
    (None, error response) when every mirror fails. ``kind`` namespaces the
    cache key (``overpass`` vs ``overpass-ctx``). Context outages degrade to
    an empty layer (warning) instead of failing the whole render.

    P2: the fetch is raced against disconnect (no partial cache on abort).
    """
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
        elements = await race_cancel(
            request, fetch_airport_polygons(query),
            endpoint=endpoint, stage="overpass", started_mono=started_mono,
        )
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


def _effective_radius_m(
    airport: dict,
    runway_rows: list[dict],
    requested_m: float,
    margin_m: float = 1000.0,
) -> float:
    """Overpass radius covering the runway ends, not just the request.

    The query is centered on the ARP, which can sit kilometers from the far
    threshold (LHBP's 13R end is ~3.7 km out) — a fixed 3000 m default then
    silently drops that end's taxiways. The authoritative endpoints are known
    before the fetch, so expand to farthest-threshold + margin (capped to
    bound the fetch). Never shrinks below the requested radius.
    """
    try:
        from backend.airports.geometry import project
        from backend.airports.ourairports import runway_endpoints

        lat0, lon0 = float(airport["latitude_deg"]), float(airport["longitude_deg"])
        need = 0.0
        for row in runway_rows:
            try:
                le_ll, he_ll, _ = runway_endpoints(row, lat0, lon0)
            except Exception:
                continue
            for ll in (le_ll, he_ll):
                x, y = project(ll[0], ll[1], lon0, lat0)
                need = max(need, math.hypot(x, y))
        if need > 0:
            return max(requested_m, min(need + margin_m, 8000.0))
    except Exception:
        pass
    return requested_m


def _bucket_radius_m(radius_m: float) -> float:
    """Round the radius up to a cache bucket.

    ``radius_m`` is a continuous UI slider and feeds both the Overpass and
    the SVG key, so every tick used to cost its own upstream round-trip.
    Rounding *up* is safe: ``_effective_radius_m`` already over-fetches by a
    kilometre, so a larger radius never loses geometry — it only stops
    neighbouring slider positions fragmenting the cache.
    """
    bucket = settings.radius_bucket_m
    if bucket <= 0:
        return radius_m
    return math.ceil(radius_m / bucket) * bucket


def _render_keys(icao: str, radius_m: float, body: RenderRequest) -> tuple[str, str]:
    """``(svg key, metadata key)`` for one diagram.

    The metadata sidecar holds the counts and rotation the response
    reports. Without it an SVG hit had to re-fetch the Overpass payload and
    run a full ``render_diagram`` just to recover ``rotation``, throwing the
    rendered document away — the cache hit cost as much as a miss.
    """
    parts = (
        icao, f"r={radius_m:.0f}",
        f"minlen={body.min_path_len_m}", f"width={body.width}",
        f"layers={','.join(sorted(body.effective_layers()))}",
        f"zoom={body.zoom}",
        f"tlabels={int(body.taxiway_labels)}",
        f"v={render_diagram_version()}",
    )
    return airports_cache_key("svg", *parts), airports_cache_key("meta", *parts)


async def _load_cached_render(
    svg_key: str, meta_key: str, warnings: list[str]
) -> tuple[str, dict[str, int], dict[str, int], float] | None:
    """Cached SVG plus its metadata, or None when either is missing.

    Both are required: the response cannot be built from the document
    alone, and re-deriving the metadata costs a full render. A hit on the
    SVG with no sidecar is therefore treated as a miss.
    """
    found = await cache_get_many([svg_key, meta_key])
    svg_text, meta_json = found.get(svg_key), found.get(meta_key)
    if svg_text is None or meta_json is None:
        return None
    try:
        meta = json.loads(meta_json)
        result = (
            svg_text, meta["path_counts"], meta["raw_counts"], meta["rotation"],
        )
    except (ValueError, KeyError):
        return None
    warnings.append("svg_cache_hit")
    return result


async def _store_render(
    svg_key: str, meta_key: str, svg_text: str,
    path_counts: dict[str, int], raw_counts: dict[str, int], rotation: float,
) -> None:
    """Store the SVG and its metadata sidecar in one pipelined write."""
    await cache_set_many({
        svg_key: svg_text,
        meta_key: json.dumps({
            "path_counts": path_counts,
            "raw_counts": raw_counts,
            "rotation": rotation,
        }),
    })


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
        return err if err is not None else _error(
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


def _build_svg(
    airport: dict,
    runway_rows: list[dict],
    freq_rows: list[dict],
    elements: list[dict],
    width: int,
    min_path_len_m: float,
    context_elements: list[dict] | None = None,
    zoom: float = 1.0,
    layers: list[str] | None = None,
    taxiway_labels: bool = False,
    cancelled: object = None,
    geoms: dict[str, list[list[tuple[float, float]]]] | None = None,
    raw_counts: dict[str, int] | None = None,
    ctx_geoms: dict[str, list[list[tuple[float, float]]]] | None = None,
) -> tuple[str, dict[str, int], dict[str, int], float, list[str],
           tuple[dict, dict] | None, dict | None]:
    from backend.airports.overpass import extract_taxiway_refs, split_context

    computed_aer: tuple[dict, dict] | None = None
    if geoms is None:
        geoms, raw_counts = split_aeroway(
            elements, cancelled=cancelled if callable(cancelled) else None)
        computed_aer = (geoms, raw_counts)
    assert raw_counts is not None
    twy_refs = extract_taxiway_refs(elements) if taxiway_labels else None
    computed_ctx: dict | None = None
    if context_elements:
        if ctx_geoms is None:
            ctx_geoms = split_context(context_elements)
            computed_ctx = ctx_geoms
        raw_counts = {
            **raw_counts,
            "context_ways": sum(len(v) for v in ctx_geoms.values()),
        }
    else:
        ctx_geoms = {}
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
        layers=layers,
        taxiway_refs=twy_refs,
        taxiway_labels=taxiway_labels,
    )
    return svg, path_counts, raw_counts, rotation, warnings, computed_aer, computed_ctx


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
        svg_text, path_counts, raw_counts, rotation = cached
        cache_hit = True
    else:
        cache_hit = False
        await check_cancelled(request, endpoint=endpoint, stage="validation", started_mono=started)
        stop = threading.Event()
        watch = start_disconnect_watcher(request, stop)
        watcher = asyncio.create_task(watch())
        try:
            built = await _render_uncached(
                airport, runway_rows, freq_rows, lat, lon, radius_m, body, warnings,
                request=request, endpoint=endpoint, started_mono=started, stop=stop,
            )
        finally:
            stop.set()
            watcher.cancel()
        if isinstance(built, JSONResponse):
            return built
        svg_text, path_counts, raw_counts, rotation, \
            computed_aer, computed_ctx, aer_key, ctx_key = built
        await check_cancelled(request, endpoint=endpoint, stage="svg_build", started_mono=started)
        await _store_splits(aer_key, ctx_key, computed_aer, computed_ctx)
        await _store_render(svg_key, meta_key, svg_text, path_counts,
                            raw_counts, rotation)

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
    like an uploaded SVG (vector branch: shading sliders are ignored,
    pen/page/label/display all apply).

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
        await check_cancelled(request, endpoint=endpoint, stage="validation", started_mono=started)
        stop = threading.Event()
        watch = start_disconnect_watcher(request, stop)
        watcher = asyncio.create_task(watch())
        try:
            built = await _render_uncached(
                airport, runway_rows, freq_rows, lat, lon, radius_m, body, warnings,
                request=request, endpoint=endpoint, started_mono=started, stop=stop,
            )
        finally:
            stop.set()
            watcher.cancel()
        if isinstance(built, JSONResponse):
            return built
        svg_text, path_counts, raw_counts, rotation, \
            computed_aer, computed_ctx, aer_key, ctx_key = built
        await check_cancelled(request, endpoint=endpoint, stage="svg_build", started_mono=started)
        await _store_splits(aer_key, ctx_key, computed_aer, computed_ctx)
        await _store_render(svg_key, meta_key, svg_text, path_counts,
                            raw_counts, rotation)

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
        return _error(404, "result_not_found", "Unknown diagram result.")
    svg_text = await cache_get(f"{KEY_PREFIX}:svg:{token.lower()}")
    if svg_text is None:
        return _error(
            404, "result_not_found",
            "Diagram result expired or unknown — re-run POST /v1/airports/render.",
        )
    return Response(content=svg_text, media_type="image/svg+xml")
