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
import re

import httpx

from backend.citymap.config import settings
from backend.citymap.layers import selectors_for
from backend.http.overpass_client import OverpassClient, OverpassConfig, OverpassError

log = logging.getLogger(__name__)

# Overpass fair-use guard: at most ``overpass_max_concurrent`` upstream
# POSTs per worker. Public mirrors are shared infra — concurrent renders
# must queue locally instead of fanning out N parallel POSTs per mirror.
# The semaphore is created lazily (asyncio primitives must not be built at
# import time) and rebuilt when the setting changes. Tests reset it via
# _reset_overpass_semaphore() and drive concurrency with a fake client.
_overpass_sem: asyncio.Semaphore | None = None
_overpass_sem_capacity: int = 0


def _overpass_semaphore() -> asyncio.Semaphore:
    global _overpass_sem, _overpass_sem_capacity
    want = max(1, int(settings.overpass_max_concurrent))
    if _overpass_sem is None or _overpass_sem_capacity != want:
        _overpass_sem = asyncio.Semaphore(want)
        _overpass_sem_capacity = want
    return _overpass_sem


def _reset_overpass_semaphore() -> None:
    """Drop the cached semaphore (tests only)."""
    global _overpass_sem, _overpass_sem_capacity
    _overpass_sem = None
    _overpass_sem_capacity = 0


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
    elements: list[dict], layers: list[str],
    cancelled: object = None,
    cancel_every: int = 2000,
) -> tuple[dict[str, list[list[tuple[float, float]]]], dict[str, int]]:
    """Group Overpass elements into lon/lat polylines per requested layer.

    Returns ``(geometries, counts)`` where geometries maps layer id to a
    list of ``[(lon, lat), ...]`` polylines (closed rings repeat their
    first node) and counts reports raw node/way/relation totals.

    ``cancelled`` is an optional ``() -> bool`` polled every ``cancel_every``
    ways (P2 checkpoint 4: aborts dense renders inside the CPU loop instead
    of running to completion after the client went away). When it fires,
    :class:`backend.cancel.ClientCancelled` is raised and the caller must
    skip cache writes.
    """
    from backend.cancel import ClientCancelled
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

    for n, (wid, (refs, tags)) in enumerate(ways.items()):
        if wid in emitted_ways:
            continue
        if cancelled is not None and n % max(1, cancel_every) == 0 and callable(cancelled) and cancelled():
            raise ClientCancelled("citymap", "svg_build")
        layer = match_way_layer(tags, layers)
        if layer is not None:
            emit(layer, refs)

    counts = {"nodes": len(nodes), "ways": len(ways), "relations": len(relations)}
    return geoms, counts


async def fetch_overpass(
    query: str,
    client: httpx.AsyncClient | None = None,
    budget_s: float | None = None,
) -> list[dict]:
    """POST the query to the first healthy Overpass mirror.

    Thin binding of citymap settings to the shared
    :class:`backend.http.overpass_client.OverpassClient`: mirror failover,
    429 parking (Retry-After, Redis-shared), retries/backoff and logging
    live there. Concurrent upstream POSTs are capped by
    ``overpass_max_concurrent`` (fair use on shared public mirrors).

    Raises :class:`OverpassError` when every mirror fails, is parked, or
    answers with non-JSON so the router can map it to a 502 envelope.
    """
    cfg = OverpassConfig(
        urls=tuple(settings.overpass_urls),
        user_agent=settings.user_agent,
        timeout_s=settings.overpass_timeout_s,
        connect_timeout_s=settings.overpass_connect_timeout_s,
        retries=settings.overpass_retries,
        retry_backoff_s=settings.overpass_retry_backoff_s,
        budget_s=settings.overpass_budget_s,
        default_park_s=settings.overpass_default_park_s,
        max_park_s=settings.overpass_max_park_s,
        fail_park_s=settings.overpass_fail_park_s,
        name="overpass",
    )
    return await OverpassClient(
        cfg, semaphore=_overpass_semaphore, error_cls=OverpassError, http=client
    ).fetch(query, budget_s)


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
