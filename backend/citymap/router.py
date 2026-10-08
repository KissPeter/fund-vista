"""Thin HTTP glue for /v1/citymap. No OSM math lives here — only:

geocode/resolve -> Redis cache -> Overpass -> split/render -> schema out,
with all failures mapped to the ``{"error": {"code", "message"}}`` envelope
(the globally registered penplot handlers already cover ``PenPlotError``
and ``RequestValidationError``, so citymap reuses both).

Endpoints share the penplot per-IP rate-limit bucket (documented as shared
in the penplot config): the POST fans out to paid-by-effort upstream calls
and must not be anonymously hammerable.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from backend.cancel import (
    ClientCancelled,
    check_cancelled,
    log_and_499,
    start_disconnect_watcher,
)
from backend.citymap.cache import KEY_PREFIX, cache_get
from backend.citymap.config import settings
from backend.citymap.fetching import _load_raw
from backend.citymap.geocode import (
    BBoxTooLargeError,
    CityNotFoundError,
    GeocodeError,
    check_bbox_span,
    geocode_city,
    search_places,
)
from backend.citymap.layers import LAYER_ORDER, LAYERS
from backend.citymap.overpass import BBox, bbox_str, split_elements
from backend.citymap.render import render_svg
from backend.citymap.render_cache import (
    _load_cached_render,
    _load_cached_split,
    _render_keys,
    _store_render,
    _store_split,
)
from backend.citymap.schemas import (
    ATTRIBUTION,
    GeocodeCandidate,
    GeocodeResponse,
    GeocodeSearchResponse,
    ImportResponse,
    LayerInfo,
    LayersResponse,
    RenderRequest,
    RenderResponse,
)
from backend.citymap.schemas import (
    BBox as BBoxSchema,
)
from backend.citymap.tiles import snap_bbox
from backend.http import error_response
from backend.penplot import imaging
from backend.penplot.errors import ErrorCode, PenPlotError
from backend.penplot.router import require_rate_limit, resolve_public_base
from backend.penplot.router import store as penplot_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/citymap", tags=["citymap-v1"])


async def _resolve_area(
    body: RenderRequest, warnings: list[str]
) -> tuple[str | None, str | None, BBox] | JSONResponse:
    """Resolve city/bbox + span guard. Returns (city, display_name, bbox)
    or an error response (unknown place, upstream down, area too large)."""
    if body.city is not None:
        city = body.city.strip()
        try:
            result = await geocode_city(city)
        except CityNotFoundError:
            return error_response(404, "city_not_found", f"No place found for '{city}'.")
        except GeocodeError as exc:
            return error_response(502, "nominatim_unavailable", f"Place lookup failed: {exc.detail}")
        if result["cache_hit"]:
            warnings.append("geocode_cache_hit")
        bbox: BBox = result["bbox"]
        display_name: str | None = result["display_name"]
    else:
        assert body.bbox is not None
        city, display_name = None, None
        bbox = body.bbox.as_tuple()
    try:
        check_bbox_span(bbox)
    except BBoxTooLargeError:
        return error_response(
            422, ErrorCode.INVALID_PARAMS,
            f"Area too large (max {settings.max_bbox_deg} deg per side) — "
            "use a district or neighbourhood instead of a whole region.",
        )
    # Snap outward to a fixed grid. The UI sends raw map.getBounds() floats,
    # which are unique to the pixel, so without this the SVG cache could
    # only ever hit on a byte-identical repeat of a previous request. The
    # snapped bbox is what gets rendered and what the response reports, so
    # clients see the area actually drawn.
    snapped = snap_bbox(bbox, settings.render_snap_deg)
    if snapped != bbox:
        warnings.append("bbox_snapped")
    return city, display_name, snapped


@router.get("/layers", response_model=LayersResponse)
async def list_layers() -> LayersResponse:
    """Selectable layers, in draw order."""
    return LayersResponse(
        layers=[
            LayerInfo(
                id=layer,
                label=LAYERS[layer]["label"],
                description=LAYERS[layer]["description"],
            )
            for layer in LAYER_ORDER
        ]
    )


@router.get("/geocode/search", response_model=GeocodeSearchResponse)
async def geocode_search(
    city: str = Query(min_length=1, max_length=120),
    limit: int = Query(default=5, ge=1, le=10),
) -> GeocodeSearchResponse | JSONResponse:
    """Search place names and return up to ``limit`` candidates.

    Same-named places (e.g. several Budapests) come back as a ranked list;
    feed the chosen candidate's ``bbox`` to render/import so the map covers
    the place the user actually picked.
    """
    try:
        result = await search_places(city, limit)
    except CityNotFoundError:
        exc = PenPlotError(status=404, code="city_not_found", message=f"No place found for '{city}'.")
        return error_response(exc.status, exc.code, exc.message)
    except GeocodeError as exc:
        err = PenPlotError(status=502, code="nominatim_unavailable", message=f"Place lookup failed: {exc.detail}")
        return error_response(err.status, err.code, err.message)
    return GeocodeSearchResponse(
        city=city,
        candidates=[
            GeocodeCandidate(
                display_name=c["display_name"],
                bbox=BBoxSchema(
                    south=c["bbox"][0], west=c["bbox"][1],
                    north=c["bbox"][2], east=c["bbox"][3],
                ),
                lat=c["lat"],
                lon=c["lon"],
                category=c.get("category", ""),
                type=c.get("type", ""),
            )
            for c in result["candidates"]
        ],
        cache_hit=result["cache_hit"],
    )


@router.get("/geocode", response_model=GeocodeResponse)
async def geocode(city: str = Query(min_length=1, max_length=120)) -> GeocodeResponse | JSONResponse:
    """Resolve a place name to its OSM bounding box (cached in Redis)."""
    try:
        result = await geocode_city(city)
    except CityNotFoundError:
        exc = PenPlotError(status=404, code="city_not_found", message=f"No place found for '{city}'.")
        return error_response(exc.status, exc.code, exc.message)
    except GeocodeError as exc:
        err = PenPlotError(status=502, code="nominatim_unavailable", message=f"Place lookup failed: {exc.detail}")
        return error_response(err.status, err.code, err.message)
    south, west, north, east = result["bbox"]
    return GeocodeResponse(
        city=city,
        display_name=result["display_name"],
        bbox=BBoxSchema(south=south, west=west, north=north, east=east),
        lat=result["lat"],
        lon=result["lon"],
        cache_hit=result["cache_hit"],
    )


async def _render_uncached(
    bbox: BBox, layers: list[str], body: RenderRequest,
    svg_key: str, counts_key: str, warnings: list[str],
    request: Request | None = None,
    endpoint: str = "/v1/citymap/render",
    started_mono: float = 0.0,
) -> tuple[str, dict[str, int], dict[str, int], bool] | JSONResponse:
    """Shared fetch-render-store path for ``render`` and ``import_map``.

    SVG-cache hit, split-cache, tile fetch, threaded SVG build, store.
    Returns ``(svg_text, path_counts, raw_counts, cache_hit)`` or an
    error response. P2 checkpoints and no-partial-cache discipline
    identical for both callers.
    """
    cached = await _load_cached_render(svg_key, counts_key, layers, warnings)
    if cached is not None:
        svg_text, path_counts, raw_counts = cached
        return svg_text, path_counts, raw_counts, True
    # 1 — before any network/CPU work.
    await check_cancelled(request, endpoint=endpoint, stage="validation", started_mono=started_mono)
    # P5: same area+layers with different width/minlen reuses the split
    # and skips the Overpass/tile fetch entirely.
    split_cache = await _load_cached_split(bbox, layers, warnings)
    raw_elements = None
    geoms_pre: dict | None = None
    raw_counts_pre: dict | None = None
    if split_cache is not None:
        geoms_pre, raw_counts_pre = split_cache
    else:
        raw_elements, err = await _load_raw(
            bbox, layers, warnings,
            request=request, endpoint=endpoint, started_mono=started_mono,
        )
        if err is not None:
            return err
        assert raw_elements is not None
    # 3 — after fetch, before SVG build.
    await check_cancelled(request, endpoint=endpoint, stage="svg_build", started_mono=started_mono)

    stop = threading.Event()
    watch = start_disconnect_watcher(request, stop)
    watcher = asyncio.create_task(watch())

    def _build() -> tuple[str, dict[str, int], dict[str, int],
                          tuple[dict, dict] | None]:
        store_me: tuple[dict, dict] | None = None
        if raw_elements is None:
            assert geoms_pre is not None and raw_counts_pre is not None
            geoms, raw_counts_ = geoms_pre, raw_counts_pre
        else:
            geoms, raw_counts_ = split_elements(
                raw_elements, layers, cancelled=stop.is_set)
            store_me = (geoms, raw_counts_)
        svg, path_counts_ = render_svg(
            geoms, bbox, layers,
            width=body.width, min_path_len_m=body.min_path_len_m,
            bearing_deg=body.bearing_deg or 0.0,
            viewport_aspect=body.viewport_aspect,
            cancelled=stop.is_set,
        )
        return svg, path_counts_, raw_counts_, store_me

    try:
        svg_text, path_counts, raw_counts, store_me = await asyncio.to_thread(_build)
    except ClientCancelled as exc:
        raise log_and_499(
            endpoint=endpoint, stage=exc.stage or "svg_build",
            request=request, started_mono=started_mono,
        )
    finally:
        stop.set()
        watcher.cancel()
    # Thread may have finished just as the client went away — do not
    # poison the cache with a run nobody will use.
    await check_cancelled(request, endpoint=endpoint, stage="svg_build", started_mono=started_mono)
    if store_me is not None:
        await _store_split(bbox, layers, store_me[0], store_me[1])
    await _store_render(svg_key, counts_key, svg_text, path_counts, raw_counts)
    return svg_text, path_counts, raw_counts, False


@router.post("/render", response_model=RenderResponse,
              dependencies=[Depends(require_rate_limit)])
async def render(body: RenderRequest, request: Request) -> RenderResponse | JSONResponse:
    """Fetch OSM data for the area and render one SVG group per layer.

    P2 cooperative cancel (4 checkpoints, same JSON shapes on success):
    1. after validation + cache lookup, before any network/CPU work;
    2. immediately before the Overpass fetch (inside ``_load_raw`` race);
    3. immediately after the fetch, before the SVG build;
    4. inside the CPU loop (every 2000 ways / polylines via ``cancelled``).
    Abort raises 499 (INFO log + ``penplot_cancelled_total``) with no cache
    write and no partial ``svg_url`` file.
    """
    endpoint = "/v1/citymap/render"
    started = time.monotonic()
    warnings: list[str] = []
    resolved = await _resolve_area(body, warnings)
    if isinstance(resolved, JSONResponse):
        return resolved
    city, display_name, bbox = resolved

    layers = list(body.layers)
    svg_key, counts_key = _render_keys(bbox, layers, body)
    built = await _render_uncached(
        bbox, layers, body, svg_key, counts_key, warnings,
        request=request, endpoint=endpoint, started_mono=started,
    )
    if isinstance(built, JSONResponse):
        return built
    _, path_counts, raw_counts, cache_hit = built

    base = resolve_public_base(request)
    token = svg_key.rsplit(":", 1)[-1]
    log.info(
        "citymap.render city=%r layers=%s paths=%d cache_hit=%s",
        city or bbox_str(bbox), ",".join(layers), sum(path_counts.values()), cache_hit,
    )
    return RenderResponse(
        city=city,
        display_name=display_name,
        bbox=BBoxSchema(south=bbox[0], west=bbox[1], north=bbox[2], east=bbox[3]),
        layers=layers,
        path_counts=path_counts,
        raw_counts=raw_counts,
        svg_url=f"{base}/v1/citymap/results/{token}",
        attribution=ATTRIBUTION,
        warnings=warnings,
        cache_hit=cache_hit,
    )


@router.post("/import", response_model=ImportResponse,
              dependencies=[Depends(require_rate_limit)])
async def import_map(body: RenderRequest, request: Request) -> ImportResponse | JSONResponse:
    """Render a city map and register it as a penplot image.

    Returns ``image_id`` — convert it with ``POST /v1/convert`` exactly
    like an uploaded SVG (vector branch: shading sliders are ignored,
    pen/page/label/display all apply). The citymap SVG is chrome-free, so
    the plotter draws only street geometry.

    Same P2 checkpoints as ``/render`` (``request`` added for disconnect
    polling; success/error JSON shapes unchanged).
    """
    endpoint = "/v1/citymap/import"
    started = time.monotonic()
    warnings: list[str] = []
    resolved = await _resolve_area(body, warnings)
    if isinstance(resolved, JSONResponse):
        return resolved
    city, display_name, bbox = resolved
    layers = list(body.layers)
    svg_key, counts_key = _render_keys(bbox, layers, body)

    # The UI calls /render and then /import with the same payload, so this
    # is nearly always the render we just produced. Reading it back beats
    # rendering the same document a second time.
    built = await _render_uncached(
        bbox, layers, body, svg_key, counts_key, warnings,
        request=request, endpoint=endpoint, started_mono=started,
    )
    if isinstance(built, JSONResponse):
        return built
    svg_text, path_counts, raw_counts, _ = built

    if sum(path_counts.values()) == 0:
        return error_response(
            422, ErrorCode.INVALID_PARAMS,
            "No map features found for these layers in this area — "
            "tick more layers or use a bigger area.",
        )
    try:
        imaging.parse_svg_vectors(svg_text.encode("utf-8"))
    except PenPlotError as exc:
        return error_response(exc.status, exc.code, exc.message)
    image_id = penplot_store.put_image_bytes(svg_text.encode("utf-8"), "svg")
    log.info(
        "citymap.import city=%r layers=%s paths=%d image=%s",
        city or bbox_str(bbox), ",".join(layers),
        sum(path_counts.values()), image_id[:12],
    )
    return ImportResponse(
        image_id=image_id,
        city=city,
        display_name=display_name,
        bbox=BBoxSchema(south=bbox[0], west=bbox[1], north=bbox[2], east=bbox[3]),
        layers=layers,
        path_counts=path_counts,
        raw_counts=raw_counts,
        attribution=ATTRIBUTION,
        warnings=warnings,
    )


@router.get("/results/{token}", dependencies=[Depends(require_rate_limit)])
async def get_result(token: str) -> Response:
    """Serve a cached rendered SVG (sha1 token from ``svg_url``)."""
    if len(token) != 40 or any(c not in "0123456789abcdef" for c in token.lower()):
        return error_response(404, "result_not_found", "Unknown map result.")
    svg_text = await cache_get(f"{KEY_PREFIX}:svg:{token.lower()}")
    if svg_text is None:
        return error_response(
            404, "result_not_found",
            "Map result expired or unknown — re-run POST /v1/citymap/render.",
        )
    return Response(content=svg_text, media_type="image/svg+xml")
