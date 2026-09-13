"""OSM Main API v0.6 /map fallback for citymap.

Different infrastructure from Overpass, so it survives Overpass outages
(triple-504 case that motivated docs/hungary-europe-map-providers.md).
Zero new dependencies: OSM XML is parsed with defusedxml (already used by
penplot imaging) into Overpass-shaped ``elements`` so
``split_elements``/``render_svg`` are reused unchanged.

Constraint: ``GET /api/0.6/map`` caps bbox area at 0.25 deg^2, so larger
bboxes are chunked into ``osm_api_max_deg`` cells and merged (dedup by id).
Per the OSM API usage policy this endpoint is for small reads — callers
must keep bboxes small and cache (router does both).
"""

from __future__ import annotations

import logging
import math

import httpx
from defusedxml import ElementTree as DET

from backend.citymap.config import settings
from backend.citymap.overpass import BBox

log = logging.getLogger(__name__)


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

    Raises :class:`OsmApiError` when every cell fails or answers unusable
    XML so the router can map it to a 502 envelope.
    """
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=settings.osm_api_timeout_s)
    try:
        south, west, north, east = bbox
        if max(north - south, east - west) > settings.max_bbox_deg:
            raise OsmApiError(
                f"bbox exceeds max_bbox_deg ({settings.max_bbox_deg})"
            )
        cells = chunk_bbox(bbox, settings.osm_api_max_deg)
        failures: list[str] = []
        parsed: list[list[dict]] = []
        for cell in cells:
            c_south, c_west, c_north, c_east = cell
            params = {
                "bbox": f"{c_west},{c_south},{c_east},{c_north}",
            }
            try:
                resp = await client.get(
                    settings.osm_api_url,
                    params=params,
                    headers={"User-Agent": settings.user_agent},
                )
                if resp.status_code != 200:
                    failures.append(
                        f"cell {params['bbox']} answered HTTP {resp.status_code}"
                    )
                    log.warning(
                        "osm_api.fail http=%d bbox=%s",
                        resp.status_code,
                        params["bbox"],
                    )
                    continue
                parsed.append(parse_osm_xml(resp.text))
            except (httpx.HTTPError, OsmApiError) as exc:
                failures.append(f"cell {params['bbox']}: {exc}")
                log.warning("osm_api.fail bbox=%s error=%r", params["bbox"], exc)
        if not parsed:
            raise OsmApiError("; ".join(failures) or "no cells fetched")
        if failures:
            log.info("osm_api.partial failures=%d cells=%d", len(failures), len(cells))
        elements = merge_elements(parsed)
        log.info("osm_api.ok cells=%d elements=%d", len(cells), len(elements))
        return elements
    finally:
        if own_client:
            await client.aclose()


__all__ = [
    "OsmApiError",
    "chunk_bbox",
    "parse_osm_xml",
    "merge_elements",
    "fetch_osm_api",
]
