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

from backend.citymap.cache import (
    KEY_PREFIX,
    cache_get,
    cache_get_many,
    cache_set_many,
    citymap_cache_key,
)
from backend.citymap.config import settings
from backend.citymap.geocode import (
    BBoxTooLargeError,
    CityNotFoundError,
    GeocodeError,
    check_bbox_span,
    geocode_city,
    search_places,
)
from backend.citymap.layers import LAYERS, LAYER_ORDER
from backend.citymap.overpass import (
    BBox,
    OverpassError,
    bbox_str,
    fetch_overpass,
    split_elements,
    union_members,
    wrap_query,
)
from backend.citymap.osm_api import OsmApiError, fetch_osm_api, merge_elements
from backend.citymap.render import render_svg
from backend.citymap.tiles import (
    assign_to_tiles,
    clip_elements,
    covering_rects,
    rect_bbox,
    snap_bbox,
    tile_deg_for_bbox,
    tile_ref,
    tiles_for_bbox,
)
from backend.citymap.schemas import (
    ATTRIBUTION,
    BBox as BBoxSchema,
    GeocodeCandidate,
    GeocodeResponse,
    GeocodeSearchResponse,
    ImportResponse,
    LayersResponse,
    LayerInfo,
    RenderRequest,
    RenderResponse,
)
from backend.penplot import imaging
from backend.penplot.errors import ErrorCode, PenPlotError
from backend.penplot.router import require_rate_limit
from backend.penplot.router import resolve_public_base
from backend.penplot.router import store as penplot_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/citymap", tags=["citymap-v1"])


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
    # Snap outward to a fixed grid. The UI sends raw map.getBounds() floats,
    # which are unique to the pixel, so without this the SVG cache could
    # only ever hit on a byte-identical repeat of a previous request. The
    # snapped bbox is what gets rendered and what the response reports, so
    # clients see the area actually drawn.
    snapped = snap_bbox(bbox, settings.render_snap_deg)
    if snapped != bbox:
        warnings.append("bbox_snapped")
    return city, display_name, snapped


def _tile_key(tile: tuple[int, int], deg: float, layer: str) -> str:
    return citymap_cache_key("tile", tile_ref(tile, deg), layer)


async def _fetch_missing(
    missing: dict[str, set[tuple[int, int]]], deg: float, warnings: list[str],
    request: Request | None = None,
    endpoint: str = "/v1/citymap/render",
    started_mono: float = 0.0,
) -> tuple[dict[str, list[dict]] | None, set[tuple[int, int]], JSONResponse | None]:
    """Fetch the missing ``(layer -> tiles)`` work in as few queries as we can.

    Missing tiles are decomposed into rectangles and unioned into a single
    Overpass query, so a cold render still costs one round-trip exactly as
    it used to — only a warm one gets cheaper.

    P2: the Overpass ``httpx`` fetch is raced against a disconnect watcher
    (``race_cancel``) — on abort the fetch task is cancelled and 499 raised
    with no partial cache write.

    Returns ``(elements per layer, tiles fully covered by the fetch, error)``.
    The covered set matters: a way whose nodes straddle the edge of the
    fetched region would otherwise be filed under an outside tile, and
    caching that tile would pin a payload missing everything else there.
    """
    all_tiles: set[tuple[int, int]] = set()
    for tiles in missing.values():
        all_tiles |= tiles
    rects = covering_rects(all_tiles)
    layers = sorted(missing)

    covered: set[tuple[int, int]] = set()
    for row0, col0, row1, col1 in rects:
        for r in range(row0, row1 + 1):
            for c in range(col0, col1 + 1):
                covered.add((r, c))

    members: list[str] = []
    for rect in rects:
        members.extend(union_members(rect_bbox(rect, deg), layers))
    try:
        # Checkpoint 2/3 boundary: race the Overpass fetch so abort stops
        # the httpx wait instead of running to completion (P2.3).
        elements = await race_cancel(
            request, fetch_overpass(wrap_query(members)),
            endpoint=endpoint, stage="overpass", started_mono=started_mono,
        )
    except OverpassError as exc:
        log.warning("citymap overpass failed, trying OSM API: %s", exc.detail)
        chunks: list[list[dict]] = []
        try:
            for rect in rects:
                chunks.append(await race_cancel(
                    request, fetch_osm_api(rect_bbox(rect, deg)),
                    endpoint=endpoint, stage="overpass",
                    started_mono=started_mono,
                ))
        except OsmApiError as exc2:
            return None, set(), _error(
                502,
                "overpass_unavailable",
                f"Map data fetch failed (Overpass: {exc.detail}; "
                f"OSM API: {exc2.detail}). "
                "(Upstreams busy — retry in a minute, with fewer layers or a smaller area).",
            )
        elements = merge_elements(chunks)
        warnings.append("osm_api_fallback")

    # One query returns every layer at once, and the OSM Main API fallback
    # is not layer-aware at all, so each tile key gets only its own layer —
    # otherwise the per-layer reuse this scheme buys would be a lie.
    per_layer = {layer: _select_layer(elements, layer) for layer in layers}
    return per_layer, covered, None


def _select_layer(elements: list[dict], layer: str) -> list[dict]:
    """Elements belonging to ``layer``, plus the nodes they reference.

    A union query returns every layer at once; each tile key must hold only
    its own layer or the per-layer reuse this whole scheme buys would be a
    lie. Uses the same predicates the renderer does.
    """
    from backend.citymap.overpass import match_relation_layer, match_way_layer

    nodes: dict[int, dict] = {}
    ways: dict[int, dict] = {}
    relations: list[dict] = []
    for el in elements:
        kind = el.get("type")
        if kind == "node":
            nodes[el["id"]] = el
        elif kind == "way":
            ways[el["id"]] = el
        elif kind == "relation":
            relations.append(el)

    only = [layer]
    kept: dict[tuple[str, int], dict] = {}

    def keep_way(way: dict) -> None:
        kept[("way", way["id"])] = way
        for ref in way.get("nodes", []):
            node = nodes.get(ref)
            if node is not None:
                kept[("node", ref)] = node

    for rel in relations:
        if match_relation_layer(rel.get("tags", {}) or {}, only) is None:
            continue
        kept[("relation", rel["id"])] = rel
        for member in rel.get("members", []):
            if member.get("type") == "way" and member.get("ref") in ways:
                keep_way(ways[member["ref"]])
    for way in ways.values():
        if match_way_layer(way.get("tags", {}) or {}, only) is not None:
            keep_way(way)
    return list(kept.values())


async def _load_raw(
    bbox: BBox, layers: list[str], warnings: list[str],
    request: Request | None = None,
    endpoint: str = "/v1/citymap/render",
    started_mono: float = 0.0,
) -> tuple[list[dict] | None, JSONResponse | None]:
    """Tile-cached fetch with OSM Main API fallback.

    Raw OSM is held per ``(tile, layer)`` rather than per
    ``(exact bbox, layer set)``, so a pan refetches only the new tiles and
    ticking a layer on reuses the layers already held. The result is
    clipped back to ``bbox`` so the element set — and therefore the
    renderer's extent — is identical to what one query over ``bbox`` would
    have produced, whatever the cache happened to hold.

    Returns (elements, None) or (None, error response) when Overpass *and*
    the OSM Main API both fail. Fallback hits append ``osm_api_fallback``
    so clients can tell the data came from chunked ``/api/0.6/map`` reads.
    """
    deg = tile_deg_for_bbox(bbox, settings.tile_max_tiles)
    tiles = tiles_for_bbox(bbox, deg)

    keys = {
        (layer, tile): _tile_key(tile, deg, layer)
        for layer in layers
        for tile in tiles
    }
    found = await cache_get_many(list(keys.values()))

    payloads: dict[tuple[str, tuple[int, int]], list[dict]] = {}
    missing: dict[str, set[tuple[int, int]]] = {}
    for (layer, tile), key in keys.items():
        raw = found.get(key)
        if raw is not None:
            try:
                payloads[(layer, tile)] = json.loads(raw)
                continue
            except ValueError:
                pass
        missing.setdefault(layer, set()).add(tile)

    hit_count = len(keys) - sum(len(v) for v in missing.values())
    if hit_count:
        warnings.append("overpass_cache_hit")

    if missing:
        per_layer, covered, err = await _fetch_missing(
            missing, deg, warnings,
            request=request, endpoint=endpoint, started_mono=started_mono,
        )
        if err is not None:
            return None, err
        assert per_layer is not None
        writes: dict[str, str] = {}
        for layer, elements in per_layer.items():
            by_tile = assign_to_tiles(elements, deg, limit_to=covered)
            # Cache every tile the fetch covered, not just the ones this
            # request lacked — the rectangle is already paid for and the
            # neighbours are what the next pan will ask for. A covered tile
            # with no features of this layer is a real answer, not a miss;
            # storing the empty list stops it being re-fetched forever.
            for tile in covered:
                payload = by_tile.get(tile, [])
                writes[_tile_key(tile, deg, layer)] = json.dumps(payload)
                if tile in missing[layer]:
                    payloads[(layer, tile)] = payload
        await cache_set_many(writes, settings.tile_cache_ttl_hours * 3600)

    # Assemble in a fixed (layer, tile) order rather than in whatever order
    # the cache answered. Assembly order decides the order of <path>
    # elements in the SVG, and the same request must render identically
    # whether it was served entirely from cache or partly refetched.
    chunks = [
        payloads.get((layer, tile), [])
        for layer in layers
        for tile in tiles
    ]
    log.info(
        "citymap.tiles z=%g tiles=%d layers=%d hit=%d miss=%d",
        deg, len(tiles), len(layers), hit_count,
        sum(len(v) for v in missing.values()),
    )
    return clip_elements(merge_elements(chunks), bbox), None


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
        return _error(exc.status, exc.code, exc.message)
    except GeocodeError as exc:
        err = PenPlotError(status=502, code="nominatim_unavailable", message=f"Place lookup failed: {exc.detail}")
        return _error(err.status, err.code, err.message)
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


def _render_keys(
    bbox: BBox, layers: list[str], body: RenderRequest
) -> tuple[str, str]:
    """``(svg key, counts key)`` for one render.

    The counts sidecar holds the path/raw totals the response reports. It
    exists so an SVG hit costs two small reads instead of re-parsing a
    multi-MB payload and re-running ``split_elements`` purely to fill in
    numbers the renderer already computed once.
    """
    parts = (
        bbox_str(bbox), ",".join(sorted(layers)),
        f"minlen={body.min_path_len_m}", f"width={body.width}",
    )
    return citymap_cache_key("svg", *parts), citymap_cache_key("counts", *parts)


async def _load_cached_render(
    svg_key: str, counts_key: str, layers: list[str], warnings: list[str]
) -> tuple[str, dict[str, int], dict[str, int]] | None:
    """Cached SVG plus its counts, or None when the SVG is not held."""
    found = await cache_get_many([svg_key, counts_key])
    svg_text = found.get(svg_key)
    if svg_text is None:
        return None
    warnings.append("svg_cache_hit")
    raw_counts: dict[str, int] = {"nodes": -1, "ways": -1, "relations": -1}
    counts_json = found.get(counts_key)
    if counts_json is not None:
        try:
            meta = json.loads(counts_json)
            return svg_text, meta["path_counts"], meta["raw_counts"]
        except (ValueError, KeyError):
            pass
    # Sidecar missing or unreadable (an SVG cached before this existed, or
    # an expiry race): count paths in the document rather than re-fetching.
    warnings.append("counts_from_cached_svg")
    return svg_text, _count_paths_in_svg(svg_text, layers), raw_counts


async def _store_render(
    svg_key: str, counts_key: str, svg_text: str,
    path_counts: dict[str, int], raw_counts: dict[str, int],
) -> None:
    """Store the SVG and its counts sidecar in one pipelined write."""
    await cache_set_many({
        svg_key: svg_text,
        counts_key: json.dumps(
            {"path_counts": path_counts, "raw_counts": raw_counts}
        ),
    })


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

    cached = await _load_cached_render(svg_key, counts_key, layers, warnings)
    if cached is not None:
        svg_text, path_counts, raw_counts = cached
        cache_hit = True
    else:
        cache_hit = False
        # 1 — before any network/CPU work.
        await check_cancelled(request, endpoint=endpoint, stage="validation", started_mono=started)
        raw_elements, err = await _load_raw(
            bbox, layers, warnings,
            request=request, endpoint=endpoint, started_mono=started,
        )
        if err is not None:
            return err
        # 3 — after fetch, before SVG build.
        await check_cancelled(request, endpoint=endpoint, stage="svg_build", started_mono=started)

        stop = threading.Event()
        watch = start_disconnect_watcher(request, stop)
        watcher = asyncio.create_task(watch())

        def _build() -> tuple[str, dict[str, int], dict[str, int]]:
            from backend.citymap.overpass import split_elements as _split
            from backend.citymap.render import render_svg as _render

            geoms, raw_counts_ = _split(
                raw_elements or [], layers, cancelled=stop.is_set)
            svg, path_counts_ = _render(
                geoms, bbox, layers,
                width=body.width, min_path_len_m=body.min_path_len_m,
                cancelled=stop.is_set,
            )
            return svg, path_counts_, raw_counts_

        try:
            svg_text, path_counts, raw_counts = await asyncio.to_thread(_build)
        except ClientCancelled as exc:
            raise log_and_499(
                endpoint=endpoint, stage=exc.stage or "svg_build",
                request=request, started_mono=started,
            )
        finally:
            stop.set()
            watcher.cancel()
        # Thread may have finished just as the client went away — do not
        # poison the cache with a run nobody will use.
        await check_cancelled(request, endpoint=endpoint, stage="svg_build", started_mono=started)
        await _store_render(svg_key, counts_key, svg_text, path_counts, raw_counts)

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
    cached = await _load_cached_render(svg_key, counts_key, layers, warnings)
    if cached is not None:
        svg_text, path_counts, raw_counts = cached
    else:
        await check_cancelled(request, endpoint=endpoint, stage="validation", started_mono=started)
        elements, err = await _load_raw(
            bbox, layers, warnings,
            request=request, endpoint=endpoint, started_mono=started,
        )
        if err is not None:
            return err
        assert elements is not None
        await check_cancelled(request, endpoint=endpoint, stage="svg_build", started_mono=started)

        stop = threading.Event()
        watch = start_disconnect_watcher(request, stop)
        watcher = asyncio.create_task(watch())

        def _build() -> tuple[str, dict[str, int], dict[str, int]]:
            from backend.citymap.overpass import split_elements as _split
            from backend.citymap.render import render_svg as _render

            geoms, raw_counts_ = _split(
                elements or [], layers, cancelled=stop.is_set)
            svg, path_counts_ = render_svg(
                geoms, bbox, layers,
                width=body.width, min_path_len_m=body.min_path_len_m,
                cancelled=stop.is_set,
            )
            return svg, path_counts_, raw_counts_

        # Off the event loop: a dense render is seconds of CPU and used to
        # block the whole worker here, unlike the /render path.
        try:
            svg_text, path_counts, raw_counts = await asyncio.to_thread(_build)
        except ClientCancelled as exc:
            raise log_and_499(
                endpoint=endpoint, stage=exc.stage or "svg_build",
                request=request, started_mono=started,
            )
        finally:
            stop.set()
            watcher.cancel()
        await check_cancelled(request, endpoint=endpoint, stage="svg_build", started_mono=started)
        await _store_render(svg_key, counts_key, svg_text, path_counts, raw_counts)

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
    svg_text = await cache_get(f"{KEY_PREFIX}:svg:{token.lower()}")
    if svg_text is None:
        return _error(
            404, "result_not_found",
            "Map result expired or unknown — re-run POST /v1/citymap/render.",
        )
    return Response(content=svg_text, media_type="image/svg+xml")
