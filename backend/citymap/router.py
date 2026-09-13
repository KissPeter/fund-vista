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
import json
import logging

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from backend.citymap.cache import (
    cache_get,
    cache_set,
    citymap_cache_key,
)
from backend.citymap.config import settings
from backend.citymap.geocode import (
    BBoxTooLargeError,
    CityNotFoundError,
    GeocodeError,
    check_bbox_span,
    geocode_city,
)
from backend.citymap.layers import LAYERS, LAYER_ORDER
from backend.citymap.overpass import (
    BBox,
    OverpassError,
    bbox_str,
    build_overpass_query,
    fetch_overpass,
    split_elements,
)
from backend.citymap.osm_api import OsmApiError, fetch_osm_api
from backend.citymap.render import render_svg
from backend.citymap.schemas import (
    ATTRIBUTION,
    BBox as BBoxSchema,
    GeocodeResponse,
    ImportResponse,
    LayersResponse,
    LayerInfo,
    RenderRequest,
    RenderResponse,
)
from backend.penplot import imaging
from backend.penplot.errors import ErrorCode, PenPlotError
from backend.penplot.router import require_rate_limit
from backend.penplot.router import store as penplot_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/citymap", tags=["citymap-v1"])


def _ttl_s() -> int:
    return settings.cache_ttl_hours * 3600


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}
    )


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
            return _error(404, "city_not_found", f"No place found for '{city}'.")
        except GeocodeError as exc:
            return _error(502, "nominatim_unavailable", f"Place lookup failed: {exc.detail}")
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
        return _error(
            422, ErrorCode.INVALID_PARAMS,
            f"Area too large (max {settings.max_bbox_deg} deg per side) — "
            "use a district or neighbourhood instead of a whole region.",
        )
    return city, display_name, bbox


async def _load_raw(
    bbox: BBox, layers: list[str], warnings: list[str]
) -> tuple[list[dict] | None, JSONResponse | None]:
    """Redis-first fetch with OSM Main API fallback.

    Returns (elements, None) or (None, error response) when Overpass *and*
    the OSM Main API both fail. Fallback hits append ``osm_api_fallback``
    so clients can tell the data came from chunked ``/api/0.6/map`` reads.
    """
    key = citymap_cache_key("overpass", bbox_str(bbox), ",".join(sorted(layers)))
    raw_json = await cache_get(key)
    if raw_json is not None:
        warnings.append("overpass_cache_hit")
        try:
            return json.loads(raw_json), None
        except ValueError:
            pass
    try:
        elements = await fetch_overpass(build_overpass_query(bbox, layers))
    except OverpassError as exc:
        log.warning("citymap overpass failed, trying OSM API: %s", exc.detail)
        try:
            elements = await fetch_osm_api(bbox)
        except OsmApiError as exc2:
            return None, _error(
                502,
                "overpass_unavailable",
                f"Map data fetch failed (Overpass: {exc.detail}; "
                f"OSM API: {exc2.detail}). "
                "(Upstreams busy — retry in a minute, with fewer layers or a smaller area).",
            )
        warnings.append("osm_api_fallback")
    await cache_set(key, json.dumps(elements))
    return elements, None


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


@router.get("/geocode", response_model=GeocodeResponse)
async def geocode(city: str = Query(min_length=1, max_length=120)) -> GeocodeResponse | JSONResponse:
    """Resolve a place name to its OSM bounding box (cached in Redis)."""
    try:
        result = await geocode_city(city)
    except CityNotFoundError:
        exc = PenPlotError(status=404, code="city_not_found", message=f"No place found for '{city}'.")
        return _error(exc.status, exc.code, exc.message)
    except GeocodeError as exc:
        err = PenPlotError(status=502, code="nominatim_unavailable", message=f"Place lookup failed: {exc.detail}")
        return _error(err.status, err.code, err.message)
    south, west, north, east = result["bbox"]
    return GeocodeResponse(
        city=city,
        display_name=result["display_name"],
        bbox=BBoxSchema(south=south, west=west, north=north, east=east),
        lat=result["lat"],
        lon=result["lon"],
        cache_hit=result["cache_hit"],
    )


@router.post("/render", response_model=RenderResponse,
              dependencies=[Depends(require_rate_limit)])
async def render(body: RenderRequest, request: Request) -> RenderResponse | JSONResponse:
    """Fetch OSM data for the area and render one SVG group per layer."""
    warnings: list[str] = []
    resolved = await _resolve_area(body, warnings)
    if isinstance(resolved, JSONResponse):
        return resolved
    city, display_name, bbox = resolved

    layers = list(body.layers)
    raw_key = citymap_cache_key("overpass", bbox_str(bbox), ",".join(sorted(layers)))
    svg_key = citymap_cache_key(
        "svg", bbox_str(bbox), ",".join(sorted(layers)),
        f"minlen={body.min_path_len_m}", f"width={body.width}",
    )

    # -- data (Redis first, Overpass on miss) -----------------------------
    cached_svg = await cache_get(svg_key)
    if cached_svg is not None:
        svg_text = cached_svg
        cache_hit = True
        warnings.append("svg_cache_hit")
        # Path counts are recomputed cheaply below from the raw payload when
        # available; otherwise they are re-derived from the cached SVG.
        raw_elements: list[dict] | None = None
        raw_json = await cache_get(raw_key)
        if raw_json is not None:
            try:
                raw_elements = json.loads(raw_json)
            except ValueError:
                raw_elements = None
    else:
        cache_hit = False
        raw_elements, err = await _load_raw(bbox, layers, warnings)
        if err is not None:
            return err

    # -- split + render (CPU-bound: off the event loop) --------------------
    def _build() -> tuple[str, dict[str, int], dict[str, int]]:
        geoms, raw_counts = split_elements(raw_elements or [], layers)
        svg, path_counts = render_svg(
            geoms, bbox, layers,
            width=body.width, min_path_len_m=body.min_path_len_m,
        )
        return svg, path_counts, raw_counts

    if cached_svg is not None and raw_elements is None:
        # SVG hit but no raw payload (e.g. raw entry expired first): count
        # paths straight from the cached document instead of re-fetching.
        svg_text = cached_svg
        path_counts = _count_paths_in_svg(cached_svg, layers)
        raw_counts = {"nodes": -1, "ways": -1, "relations": -1}
        warnings.append("counts_from_cached_svg")
    elif cached_svg is not None:
        # Both cached: reuse the SVG, split the raw payload for counts only
        # (no re-render — the stored document is already what we would emit).
        assert raw_elements is not None
        svg_text = cached_svg
        geoms, raw_counts = await asyncio.to_thread(
            split_elements, raw_elements, layers
        )
        path_counts = {layer: len(geoms.get(layer, [])) for layer in layers}
    else:
        svg_text, path_counts, raw_counts = await asyncio.to_thread(_build)
        await cache_set(svg_key, svg_text)

    base = str(request.base_url).rstrip("/")
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
async def import_map(body: RenderRequest) -> ImportResponse | JSONResponse:
    """Render a city map and register it as a penplot image.

    Returns ``image_id`` — convert it with ``POST /v1/convert`` exactly
    like an uploaded SVG (vector branch: shading sliders are ignored,
    pen/page/label/display all apply). The citymap SVG is chrome-free, so
    the plotter draws only street geometry.
    """
    warnings: list[str] = []
    resolved = await _resolve_area(body, warnings)
    if isinstance(resolved, JSONResponse):
        return resolved
    city, display_name, bbox = resolved
    layers = list(body.layers)
    elements, err = await _load_raw(bbox, layers, warnings)
    if err is not None:
        return err
    assert elements is not None
    geoms, raw_counts = await asyncio.to_thread(split_elements, elements, layers)
    svg_text, path_counts = render_svg(
        geoms, bbox, layers,
        width=body.width, min_path_len_m=body.min_path_len_m,
    )
    if sum(path_counts.values()) == 0:
        return _error(
            422, ErrorCode.INVALID_PARAMS,
            "No map features found for these layers in this area — "
            "tick more layers or use a bigger area.",
        )
    try:
        imaging.parse_svg_vectors(svg_text.encode("utf-8"))
    except PenPlotError as exc:
        return _error(exc.status, exc.code, exc.message)
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


def _count_paths_in_svg(svg_text: str, layers: list[str]) -> dict[str, int]:
    """Fallback path counter for the cached-SVG-only branch."""
    counts = {layer: 0 for layer in layers}
    for layer in layers:
        marker = f'id="citymap-{layer}"'
        start = svg_text.find(marker)
        if start == -1:
            continue
        group_end = svg_text.find("</g>", start)
        segment = svg_text[start:group_end] if group_end != -1 else svg_text[start:]
        counts[layer] = segment.count("<path")
    return counts


@router.get("/results/{token}", dependencies=[Depends(require_rate_limit)])
async def get_result(token: str) -> Response:
    """Serve a cached rendered SVG (sha1 token from ``svg_url``)."""
    if len(token) != 40 or any(c not in "0123456789abcdef" for c in token.lower()):
        return _error(404, "result_not_found", "Unknown map result.")
    svg_text = await cache_get(f"fund-vista:citymap:v1:svg:{token.lower()}")
    if svg_text is None:
        return _error(
            404, "result_not_found",
            "Map result expired or unknown — re-run POST /v1/citymap/render.",
        )
    return Response(content=svg_text, media_type="image/svg+xml")
