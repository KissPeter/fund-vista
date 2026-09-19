"""Overpass API access: query building, fetching, and layer splitting.

Query shape follows the city-roads console recipes (union of filtered
ways/relations in a bbox, ``out body; >; out skel qt;`` so node geometry
arrives in the same payload). Splitting assigns every way to exactly one
requested layer — relation members (ferry/rail routes, multipolygon
outers+inners) first, then first-match in ``LAYER_ORDER`` — so combined
layers never double-ink a street.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re

import httpx

from backend.citymap.config import settings
from backend.citymap.layers import selectors_for

log = logging.getLogger(__name__)

# HTTP statuses worth retrying on the same mirror before failing over.
# 400/403/404 fail fast (retrying won't help: rejected query / blocked).
_RETRYABLE_STATUS: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
_BODY_SNIPPET_LEN = 200

BBox = tuple[float, float, float, float]  # south, west, north, east

_HIGHWAY_MAJOR = re.compile(r"^(motorway|trunk|primary)(_link)?$")
_HIGHWAY_MINOR = re.compile(
    r"^(secondary|tertiary|unclassified|residential|living_street|pedestrian|service|track|road)(_link)?$"
)
_HIGHWAY_PATH = re.compile(r"^(footway|path|cycleway|steps|bridleway|corridor)$")
_RAIL_TRACK = re.compile(r"^(rail|light_rail|subway|tram|narrow_gauge|monorail|preserved)$")
_WATERWAY_FLOW = re.compile(r"^(river|stream|canal|ditch|drain)$")
_RAIL_ROUTE = re.compile(r"^(train|tram|subway|light_rail)$")


def bbox_str(bbox: BBox) -> str:
    south, west, north, east = bbox
    return f"{south:.5f},{west:.5f},{north:.5f},{east:.5f}"


def union_members(bbox: BBox, layers: list[str]) -> list[str]:
    """Overpass QL union members for ``layers`` inside ``bbox``.

    Every member gets exactly one trailing ``;`` — Overpass rejects the
    whole query (400/406) when a member is unterminated. Split out from
    :func:`build_overpass_query` so the tile loader can union members from
    several bboxes into a single query.
    """
    ways, relations = selectors_for(layers)
    return [
        s.format(bbox=bbox_str(bbox)).rstrip().rstrip(";") + ";"
        for s in ways + relations
    ]


def wrap_query(members: list[str], timeout_s: int = 25) -> str:
    """Wrap union members in the standard recursing-out envelope."""
    block = "\n  ".join(members)
    return (
        f"[out:json][timeout:{timeout_s}];\n"
        f"(\n  {block}\n);\n"
        "out body;\n"
        ">;\n"
        "out skel qt;"
    )


def build_overpass_query(bbox: BBox, layers: list[str], timeout_s: int = 25) -> str:
    """Render the Overpass QL union for ``layers`` inside ``bbox``."""
    return wrap_query(union_members(bbox, layers), timeout_s)


def match_way_layer(tags: dict, layers: list[str]) -> str | None:
    """First layer in ``LAYER_ORDER`` whose predicate matches ``tags``."""
    wanted = set(layers)
    highway = tags.get("highway", "")
    if "aeroway" in wanted and tags.get("aeroway"):
        return "aeroway"
    if "rails" in wanted and _RAIL_TRACK.match(tags.get("railway", "")):
        return "rails"
    if "highways" in wanted and _HIGHWAY_MAJOR.match(highway):
        return "highways"
    if "roads" in wanted and _HIGHWAY_MINOR.match(highway):
        return "roads"
    if "paths" in wanted and _HIGHWAY_PATH.match(highway):
        return "paths"
    if "waterway" in wanted and _WATERWAY_FLOW.match(tags.get("waterway", "")):
        return "waterway"
    if "water" in wanted and (tags.get("natural") == "water" or "water" in tags):
        return "water"
    if "buildings" in wanted and "building" in tags:
        return "buildings"
    return None


def match_relation_layer(tags: dict, layers: list[str]) -> str | None:
    wanted = set(layers)
    route = tags.get("route", "")
    if "ferry" in wanted and route == "ferry":
        return "ferry"
    if "rails" in wanted and _RAIL_ROUTE.match(route):
        return "rails"
    if tags.get("type") == "multipolygon":
        if "water" in wanted and (tags.get("natural") == "water" or "water" in tags):
            return "water"
        if "buildings" in wanted and "building" in tags:
            return "buildings"
    return None


def split_elements(
    elements: list[dict], layers: list[str]
) -> tuple[dict[str, list[list[tuple[float, float]]]], dict[str, int]]:
    """Group Overpass elements into lon/lat polylines per requested layer.

    Returns ``(geometries, counts)`` where geometries maps layer id to a
    list of ``[(lon, lat), ...]`` polylines (closed rings repeat their
    first node) and counts reports raw node/way/relation totals.
    """
    nodes: dict[int, tuple[float | None, float | None]] = {}
    ways: dict[int, tuple[list[int], dict]] = {}
    relations: list[dict] = []
    for el in elements:
        kind = el.get("type")
        if kind == "node":
            nodes[el["id"]] = (el.get("lon"), el.get("lat"))
        elif kind == "way":
            ways[el["id"]] = (el.get("nodes", []), el.get("tags", {}) or {})
        elif kind == "relation":
            relations.append(el)

    geoms: dict[str, list[list[tuple[float, float]]]] = {layer: [] for layer in layers}
    emitted_ways: set[int] = set()

    def emit(layer: str, refs: list[int]) -> None:
        pts: list[tuple[float, float]] = []
        for ref in refs:
            coord = nodes.get(ref)
            if coord is None or coord[0] is None or coord[1] is None:
                continue
            pts.append((coord[0], coord[1]))
        if len(pts) >= 2:
            geoms[layer].append(pts)

    # Relations first so route/multipolygon members are claimed before the
    # generic way pass (no double-inking across layers).
    for rel in relations:
        layer = match_relation_layer(rel.get("tags", {}) or {}, layers)
        if layer is None:
            continue
        for member in rel.get("members", []):
            if member.get("type") == "way" and member.get("ref") in ways:
                ref = member["ref"]
                emit(layer, ways[ref][0])
                emitted_ways.add(ref)

    for wid, (refs, tags) in ways.items():
        if wid in emitted_ways:
            continue
        layer = match_way_layer(tags, layers)
        if layer is not None:
            emit(layer, refs)

    counts = {"nodes": len(nodes), "ways": len(ways), "relations": len(relations)}
    return geoms, counts


async def fetch_overpass(query: str, client: httpx.AsyncClient | None = None) -> list[dict]:
    """POST the query to the first healthy Overpass mirror.

    Each mirror gets ``overpass_retries`` extra attempts on retryable
    failures (HTTP 408/429/5xx + network errors) with exponential backoff
    before failing over to the next mirror. 400/403/404 fail fast.

    Raises :class:`OverpassError` when every mirror fails or answers with
    non-JSON so the router can map it to a 502 envelope.
    """
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=settings.overpass_timeout_s)
    try:
        failures: list[str] = []
        max_attempts = max(1, settings.overpass_retries + 1)
        for url in settings.overpass_urls:
            for attempt in range(max_attempts):
                try:
                    # Overpass answers 406 to generic HTTP-client UAs
                    # (e.g. python-httpx/*) — identify the app on every POST.
                    resp = await client.post(
                        url,
                        data={"data": query},
                        headers={"User-Agent": settings.user_agent},
                    )
                    if resp.status_code != 200:
                        detail = f"{url} answered HTTP {resp.status_code}"
                        snippet = _body_snippet(resp)
                        if snippet:
                            detail += f" ({snippet})"
                        retryable = resp.status_code in _RETRYABLE_STATUS
                        if retryable and attempt + 1 < max_attempts:
                            log.warning(
                                "overpass.retry mirror=%s http=%d attempt=%d/%d",
                                url, resp.status_code, attempt + 1, max_attempts,
                            )
                            await asyncio.sleep(
                                _backoff(settings.overpass_retry_backoff_s, attempt)
                            )
                            continue
                        failures.append(detail)
                        log.warning("overpass.fail mirror=%s http=%d", url, resp.status_code)
                        break
                    try:
                        payload = resp.json()
                    except ValueError as exc:
                        detail = f"{url} returned an invalid payload ({_format_exc(exc)})"
                        if attempt + 1 < max_attempts:
                            log.warning(
                                "overpass.retry mirror=%s invalid_payload attempt=%d/%d",
                                url, attempt + 1, max_attempts,
                            )
                            await asyncio.sleep(
                                _backoff(settings.overpass_retry_backoff_s, attempt)
                            )
                            continue
                        failures.append(detail)
                        log.warning("overpass.fail mirror=%s invalid_payload", url)
                        break
                    if not isinstance(payload, dict) or "elements" not in payload:
                        failures.append(f"{url} returned an invalid payload")
                        log.warning("overpass.fail mirror=%s invalid_payload", url)
                        break
                    log.info("overpass.ok mirror=%s elements=%d", url, len(payload["elements"]))
                    return payload["elements"]
                except (httpx.HTTPError, ValueError) as exc:
                    detail = f"{url}: {_format_exc(exc)}"
                    if attempt + 1 < max_attempts:
                        log.warning(
                            "overpass.retry mirror=%s error=%r attempt=%d/%d",
                            url, exc, attempt + 1, max_attempts,
                        )
                        await asyncio.sleep(
                            _backoff(settings.overpass_retry_backoff_s, attempt)
                        )
                        continue
                    failures.append(detail)
                    log.warning("overpass.fail mirror=%s error=%r", url, exc)
                    break
        raise OverpassError("; ".join(failures) or "no mirrors configured")
    finally:
        if own_client:
            await client.aclose()


def _format_exc(exc: BaseException) -> str:
    """Never return an empty string (httpx timeouts often stringify to '')."""
    msg = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {msg}" if msg else f"{name} (no detail)"


def _body_snippet(resp: httpx.Response) -> str:
    """First ~200 chars of an error body for diagnostics (403/504 pages)."""
    try:
        text = resp.text.strip().replace("\n", " ")
    except Exception:
        return ""
    if len(text) > _BODY_SNIPPET_LEN:
        return text[:_BODY_SNIPPET_LEN] + "…"
    return text


def _backoff(base_s: float, attempt: int) -> float:
    """Exponential backoff with a small jitter so mirrors don't thunder."""
    return base_s * (2**attempt) + random.uniform(0, 0.25)


class OverpassError(Exception):
    """All Overpass mirrors failed or answered unusable payloads."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


__all__ = [
    "BBox",
    "bbox_str",
    "build_overpass_query",
    "match_way_layer",
    "match_relation_layer",
    "split_elements",
    "fetch_overpass",
    "OverpassError",
]
