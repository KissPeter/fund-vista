"""OSM Main API v0.6 /map fallback for citymap.

Different infrastructure from Overpass, so it survives Overpass outages
(triple-504 case that motivated docs/hungary-europe-map-providers.md).
Zero new dependencies: OSM XML is parsed with defusedxml (already used by
penplot imaging) into Overpass-shaped ``elements`` so
``split_elements``/``render_svg`` are reused unchanged.

Constraint: ``GET /api/0.6/map`` caps bbox area at 0.25 deg^2 *and* node
count (~50k — dense city centers 400 well below the area limit), so bboxes
are chunked into ``osm_api_max_deg`` cells and 400-cells are subdivided
adaptively into quadrants down to ``osm_api_min_deg`` before giving up.
Per the OSM API usage policy this endpoint is for small reads — callers
must keep bboxes small and cache (router does both).
"""

from __future__ import annotations

import logging
import math
import re

import httpx
from defusedxml import ElementTree as DET

from backend.citymap.config import settings
from backend.citymap.overpass import BBox
from backend.http import upstream
from backend.http.mirror_health import osm_api_mirrors

log = logging.getLogger(__name__)

# Retries/backoff/Retry-After/logging live in the shared smart client
# (backend.http.upstream). 400 = too many nodes / bbox invalid -> subdivide
# (dense) or fail fast. 429 parks the endpoint (see _get_cell_elements).
_BODY_SNIPPET_LEN = 200
_RETRY_IN_RE = re.compile(r"try again in\s+(\d+(?:\.\d+)?)\s*seconds?", re.I)


class OsmApiError(Exception):
    """OSM Main API failed (network, non-200, or unusable XML)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def chunk_bbox(bbox: BBox, max_deg: float) -> list[BBox]:
    """Split ``bbox`` into cells no wider/taller than ``max_deg``."""
    south, west, north, east = bbox
    span_lat = max(north - south, 1e-9)
    span_lon = max(east - west, 1e-9)
    n_lat = max(1, math.ceil(span_lat / max_deg))
    n_lon = max(1, math.ceil(span_lon / max_deg))
    cells: list[BBox] = []
    for i in range(n_lat):
        c_south = south + span_lat * i / n_lat
        c_north = south + span_lat * (i + 1) / n_lat
        for j in range(n_lon):
            c_west = west + span_lon * j / n_lon
            c_east = west + span_lon * (j + 1) / n_lon
            cells.append((c_south, c_west, c_north, c_east))
    return cells


def parse_osm_xml(xml_text: str) -> list[dict]:
    """Parse OSM v0.6 XML into Overpass-shaped elements.

    Nodes carry lon/lat; ways carry ordered node refs + tags; relations
    carry tags + typed members — exactly what ``split_elements`` consumes.
    """
    try:
        root = DET.fromstring(xml_text.encode("utf-8"))
    except Exception as exc:
        raise OsmApiError(f"OSM API returned invalid XML: {exc}") from exc
    elements: list[dict] = []
    for node in root.iter("node"):
        try:
            nid = int(node.get("id", "0"))
            lon = float(node.get("lon", ""))
            lat = float(node.get("lat", ""))
        except (TypeError, ValueError):
            continue
        elements.append({"type": "node", "id": nid, "lon": lon, "lat": lat})
    for way in root.iter("way"):
        try:
            wid = int(way.get("id", "0"))
        except (TypeError, ValueError):
            continue
        refs = [int(nd.get("ref", "0")) for nd in way.iter("nd")]
        refs = [r for r in refs if r]
        tags = {
            t.get("k"): t.get("v")
            for t in way.iter("tag")
            if t.get("k") is not None
        }
        elements.append({"type": "way", "id": wid, "nodes": refs, "tags": tags})
    for rel in root.iter("relation"):
        try:
            rid = int(rel.get("id", "0"))
        except (TypeError, ValueError):
            continue
        tags = {
            t.get("k"): t.get("v")
            for t in rel.iter("tag")
            if t.get("k") is not None
        }
        members = []
        for m in rel.iter("member"):
            try:
                ref = int(m.get("ref", "0"))
            except (TypeError, ValueError):
                continue
            if m.get("type") in ("node", "way", "relation") and ref:
                members.append(
                    {"type": m.get("type"), "ref": ref, "role": m.get("role", "")}
                )
        elements.append(
            {"type": "relation", "id": rid, "tags": tags, "members": members}
        )
    return elements


class PartialElements(list):
    """Elements from an OSM API read in which some cells failed.

    A plain ``list`` to every consumer; ``failed_cells`` names the cells
    whose data is missing so callers can refuse to treat the area as covered.
    """

    failed_cells: list

    def __init__(self, elements=(), failed_cells=()):
        super().__init__(elements)
        self.failed_cells = list(failed_cells)


def merge_elements(chunks: list[list[dict]]) -> list[dict]:
    """Merge per-cell elements, deduping by (type, id)."""
    seen: set[tuple[str, int]] = set()
    merged: list[dict] = []
    for elements in chunks:
        for el in elements:
            key = (el.get("type", ""), el.get("id", 0))
            if key in seen:
                continue
            seen.add(key)
            merged.append(el)
    return merged


async def fetch_osm_api(
    bbox: BBox, client: httpx.AsyncClient | None = None
) -> list[dict]:
    """GET bbox cells from the OSM Main API and merge into elements.

    Cells that answer HTTP 400 (node-density limit in city centers) are
    subdivided into quadrants down to ``osm_api_min_deg`` before giving up.
    Transient failures are retried by the shared smart client. A 429 is
    never retried: the endpoint is parked for the server's hint and every
    call fails fast while it is parked.

    Raises :class:`OsmApiError` when every cell fails or answers unusable
    XML so the router can map it to a 502 envelope.
    """
    own_client = client is None
    client = client or upstream.smart_client(
        "osm-api",
        timeout=settings.osm_api_timeout_s,
        retries=settings.osm_api_retries,
        backoff_s=settings.osm_api_retry_backoff_s,
    )
    try:
        south, west, north, east = bbox
        if max(north - south, east - west) > settings.max_bbox_deg:
            raise OsmApiError(
                f"bbox exceeds max_bbox_deg ({settings.max_bbox_deg})"
            )
        await _raise_if_parked()
        cells = chunk_bbox(bbox, settings.osm_api_max_deg)
        failures: list[str] = []
        failed_cells: list[BBox] = []
        parsed: list[list[dict]] = []
        for cell in cells:
            await _fetch_recursive(cell, client, failures, parsed, failed_cells)
        if not parsed:
            raise OsmApiError("; ".join(failures) or "no cells fetched")
        if failures:
            log.info("osm_api.partial failures=%d cells=%d", len(failures), len(cells))
        elements = merge_elements(parsed)
        log.info("osm_api.ok cells=%d elements=%d", len(cells), len(elements))
        if failed_cells:
            return PartialElements(elements, failed_cells)
        return elements
    finally:
        if own_client:
            await client.aclose()


class _DenseCellError(Exception):
    """A cell answered HTTP 400 (too many nodes) and may succeed subdivided."""

    def __init__(self, detail: str, snippet: str = "") -> None:
        super().__init__(detail)
        self.detail = detail
        self.snippet = snippet


def split_quadrants(cell: BBox) -> list[BBox]:
    """Split a cell into 4 quadrants (used on 400/too-many-nodes)."""
    south, west, north, east = cell
    mid_lat = (south + north) / 2
    mid_lon = (west + east) / 2
    return [
        (south, west, mid_lat, mid_lon),
        (south, mid_lon, mid_lat, east),
        (mid_lat, west, north, mid_lon),
        (mid_lat, mid_lon, north, east),
    ]


def _cell_too_small(cell: BBox, min_deg: float) -> bool:
    south, west, north, east = cell
    return (north - south) <= min_deg + 1e-12 and (east - west) <= min_deg + 1e-12


def _cell_param(cell: BBox) -> str:
    c_south, c_west, c_north, c_east = cell
    return f"{c_west},{c_south},{c_east},{c_north}"


async def _fetch_recursive(
    cell: BBox,
    client: httpx.AsyncClient,
    failures: list[str],
    parsed: list[list[dict]],
    failed_cells: list[BBox] | None = None,
) -> None:
    """Fetch one cell, subdividing on 400 down to ``osm_api_min_deg``."""
    try:
        parsed.append(await _get_cell_elements(cell, client))
    except _DenseCellError as exc:
        if _cell_too_small(cell, settings.osm_api_min_deg):
            failures.append(f"cell {_cell_param(cell)} answered HTTP 400 ({exc.snippet})" if exc.snippet else f"cell {_cell_param(cell)} answered HTTP 400")
            log.warning("osm_api.fail bbox=%s dense_min_size", _cell_param(cell))
            if failed_cells is not None:
                failed_cells.append(cell)
            return
        log.info("osm_api.subdivide bbox=%s", _cell_param(cell))
        for quad in split_quadrants(cell):
            await _fetch_recursive(quad, client, failures, parsed, failed_cells)
    except (httpx.HTTPError, OsmApiError) as exc:
        failures.append(f"cell {_cell_param(cell)}: {exc!r}")
        log.warning("osm_api.fail bbox=%s error=%r", _cell_param(cell), exc)
        if failed_cells is not None:
            failed_cells.append(cell)


async def _raise_if_parked() -> None:
    """Fail without touching the network while the endpoint is parked."""
    _, parked = await osm_api_mirrors.split([settings.osm_api_url])
    if parked:
        raise OsmApiError(
            f"OSM API parked after HTTP 429 for another {math.ceil(parked[0][1])}s"
        )


def _cooldown_seconds(resp: httpx.Response) -> float:
    """Server hint (Retry-After, else "try again in N seconds"), clamped."""
    hint = upstream.retry_after_seconds(resp)
    if hint is None:
        match = _RETRY_IN_RE.search(_body_snippet(resp, limit=1000))
        hint = float(match.group(1)) if match else None
    if hint is None or hint <= 0:
        hint = settings.osm_api_cooldown_s
    return min(hint, settings.osm_api_max_cooldown_s)


async def _get_cell_elements(cell: BBox, client: httpx.AsyncClient) -> list[dict]:
    """GET one cell. 400 -> _DenseCellError (subdivide); 429 -> park + fail."""
    param = _cell_param(cell)
    await _raise_if_parked()
    try:
        resp = await client.get(
            settings.osm_api_url,
            params={"bbox": param},
            headers={"User-Agent": settings.user_agent},
        )
    except httpx.HTTPError as exc:
        raise OsmApiError(f"cell {param}: {exc!r}") from exc
    if resp.status_code == 400:
        raise _DenseCellError(f"cell {param} answered HTTP 400", _body_snippet(resp))
    if resp.status_code == 429:
        seconds = _cooldown_seconds(resp)
        await osm_api_mirrors.park(settings.osm_api_url, seconds)
        log.warning("osm_api.parked http=429 bbox=%s seconds=%.0f", param, seconds)
        raise OsmApiError(
            f"cell {param} answered HTTP 429; OSM API parked for {math.ceil(seconds)}s"
        )
    if resp.status_code != 200:
        msg = f"cell {param} answered HTTP {resp.status_code}"
        snippet = _body_snippet(resp)
        if snippet:
            msg += f" ({snippet})"
        log.warning("osm_api.fail http=%d bbox=%s", resp.status_code, param)
        raise OsmApiError(msg)
    return parse_osm_xml(resp.text)


def _body_snippet(resp: httpx.Response, limit: int = _BODY_SNIPPET_LEN) -> str:
    """First ~200 chars of an error body (e.g. 'too many nodes')."""
    try:
        text = resp.text.strip().replace("\n", " ")
    except Exception:
        return ""
    if len(text) > limit:
        return text[:limit] + "…"
    return text


__all__ = [
    "OsmApiError",
    "PartialElements",
    "chunk_bbox",
    "split_quadrants",
    "parse_osm_xml",
    "merge_elements",
    "fetch_osm_api",
]
