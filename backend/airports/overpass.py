"""Overpass ``around`` query for airport ground polygons (ODbL).

Unlike the citymap bbox query (``out body; >; out skel qt;`` with node refs),
the airport query uses ``out geom`` so ways arrive with inline coordinates —
simpler for a fixed-radius fetch and independent of citymap's splitter
(which is bbox/node-ref shaped and must stay untouched).
"""

from __future__ import annotations

import asyncio
import logging
import random

import httpx

from backend.airports.config import settings

log = logging.getLogger(__name__)

_RETRYABLE_STATUS: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
_BODY_SNIPPET_LEN = 200

# aeroway tag values we draw. ``taxilane`` folds into taxiways (same
# centerline treatment); stands/stopways get their own thin-outline groups.
# Deliberately excluded (poster-scale clutter): jet_bridge, helipad,
# navigationaid, gate.
AEROWAY_CLASSES = ("runway", "taxiway", "apron", "terminal", "hangar",
                   "stands", "stopways")

_AEROWAY_TO_CLASS = {
    "taxilane": "taxiway",
    "parking_position": "stands",
    "stopway": "stopways",
}


class AirportOverpassError(Exception):
    """All Overpass mirrors failed or answered unusable payloads."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def build_airport_query(lat: float, lon: float, radius_m: float) -> str:
    """Overpass QL: all ``aeroway`` ways+relations around the airport center."""
    r = int(radius_m)
    return (
        "[out:json][timeout:25];\n"
        "(\n"
        f'  way["aeroway"](around:{r},{lat:.6f},{lon:.6f});\n'
        f'  relation["aeroway"](around:{r},{lat:.6f},{lon:.6f});\n'
        ");\n"
        "out geom;"
    )


def split_aeroway(
    elements: list[dict],
) -> tuple[dict[str, list[list[tuple[float, float]]]], dict[str, int]]:
    """Group ``out geom`` elements by ``aeroway`` tag.

    Returns ``(geoms, counts)`` where geoms maps each class in
    ``AEROWAY_CLASSES`` to ``[[(lon, lat), ...]]`` rings/lines and counts
    reports raw element totals. Ways whose tag is an aeroway value outside
    the drawn classes (e.g. ``helipad``, ``jet_bridge``, ``gate``) are ignored.
    """
    geoms: dict[str, list[list[tuple[float, float]]]] = {
        cls: [] for cls in AEROWAY_CLASSES
    }
    n_nodes = n_ways = n_relations = 0
    for el in elements:
        kind = el.get("type")
        tags = el.get("tags", {}) or {}
        raw_cls = (tags.get("aeroway") or "").strip()
        cls = _AEROWAY_TO_CLASS.get(raw_cls, raw_cls)
        if cls not in geoms:
            continue
        if kind == "node":
            # Stands are sometimes bare nodes: synthesize a ~6 m diamond so
            # they still draw (a bare point has no plottable geometry).
            n_nodes += 1
            lon, lat = el.get("lon"), el.get("lat")
            if lon is None or lat is None:
                continue
            import math as _math

            dlat = 6.0 / 110540.0
            dlon = 6.0 / (111320.0 * _math.cos(_math.radians(lat)))
            geoms[cls].append([
                (lon, lat - dlat), (lon + dlon, lat),
                (lon, lat + dlat), (lon - dlon, lat), (lon, lat - dlat),
            ])
        elif kind == "way":
            n_ways += 1
            pts = [
                (pt["lon"], pt["lat"])
                for pt in el.get("geometry", []) or []
                if pt.get("lon") is not None and pt.get("lat") is not None
            ]
            if len(pts) >= 2:
                geoms[cls].append(pts)
        elif kind == "relation":
            n_relations += 1
            for member in el.get("members", []) or []:
                pts = [
                    (pt["lon"], pt["lat"])
                    for pt in member.get("geometry", []) or []
                    if isinstance(pt, dict)
                    and pt.get("lon") is not None
                    and pt.get("lat") is not None
                ]
                if len(pts) >= 2:
                    geoms[cls].append(pts)
    return geoms, {"nodes": n_nodes, "ways": n_ways, "relations": n_relations}


# Surrounding street context (F-002, opt-in via RenderRequest.context):
# service/access roads + buildings + standing water around the center,
# drawn faintest underneath the airfield geometry.
CONTEXT_CLASSES = ("roads", "buildings", "water")


def build_context_query(lat: float, lon: float, radius_m: float) -> str:
    """Overpass QL: streets, buildings and water around the airport center."""
    r = int(radius_m)
    at = f"(around:{r},{lat:.6f},{lon:.6f})"
    return (
        "[out:json][timeout:25];\n"
        "(\n"
        f'  way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|service|track|road)(_link)?$"]{at};\n'
        f'  way["building"]{at};\n'
        f'  way["natural"="water"]{at};\n'
        ");\n"
        "out geom;"
    )


def split_context(
    elements: list[dict],
) -> dict[str, list[list[tuple[float, float]]]]:
    """Group ``out geom`` context elements into roads/buildings/water rings."""
    geoms: dict[str, list[list[tuple[float, float]]]] = {
        cls: [] for cls in CONTEXT_CLASSES
    }
    for el in elements:
        if el.get("type") != "way":
            continue
        tags = el.get("tags", {}) or {}
        if "highway" in tags:
            cls = "roads"
        elif "building" in tags:
            cls = "buildings"
        elif tags.get("natural") == "water" or "water" in tags:
            cls = "water"
        else:
            continue
        pts = [
            (pt["lon"], pt["lat"])
            for pt in el.get("geometry", []) or []
            if pt.get("lon") is not None and pt.get("lat") is not None
        ]
        if len(pts) >= 2:
            geoms[cls].append(pts)
    return geoms


async def fetch_airport_polygons(
    query: str, client: httpx.AsyncClient | None = None
) -> list[dict]:
    """POST the query to the first healthy Overpass mirror.

    Same failover shape as citymap (per-mirror retries on retryable
    failures with exponential backoff, fail-fast on 400/403/404).
    Raises :class:`AirportOverpassError` when every mirror fails.
    """
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=settings.overpass_timeout_s)
    try:
        failures: list[str] = []
        max_attempts = max(1, settings.overpass_retries + 1)
        for url in settings.overpass_urls:
            for attempt in range(max_attempts):
                try:
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
                        if (
                            resp.status_code in _RETRYABLE_STATUS
                            and attempt + 1 < max_attempts
                        ):
                            await asyncio.sleep(
                                _backoff(settings.overpass_retry_backoff_s, attempt)
                            )
                            continue
                        failures.append(detail)
                        break
                    try:
                        payload = resp.json()
                    except ValueError as exc:
                        if attempt + 1 < max_attempts:
                            await asyncio.sleep(
                                _backoff(settings.overpass_retry_backoff_s, attempt)
                            )
                            continue
                        failures.append(f"{url} returned an invalid payload ({exc})")
                        break
                    if not isinstance(payload, dict) or "elements" not in payload:
                        failures.append(f"{url} returned an invalid payload")
                        break
                    return payload["elements"]
                except (httpx.HTTPError, ValueError) as exc:
                    if attempt + 1 < max_attempts:
                        await asyncio.sleep(
                            _backoff(settings.overpass_retry_backoff_s, attempt)
                        )
                        continue
                    failures.append(f"{url}: {type(exc).__name__}: {exc}")
                    break
        raise AirportOverpassError("; ".join(failures) or "no mirrors configured")
    finally:
        if own_client:
            await client.aclose()


def _body_snippet(resp: httpx.Response) -> str:
    try:
        text = resp.text.strip().replace("\n", " ")
    except Exception:
        return ""
    return text[:_BODY_SNIPPET_LEN] + "…" if len(text) > _BODY_SNIPPET_LEN else text


def _backoff(base_s: float, attempt: int) -> float:
    return base_s * (2**attempt) + random.uniform(0, 0.25)


__all__ = [
    "AEROWAY_CLASSES",
    "CONTEXT_CLASSES",
    "AirportOverpassError",
    "build_airport_query",
    "build_context_query",
    "fetch_airport_polygons",
    "split_aeroway",
    "split_context",
]
