"""Upstream fetch helpers for /v1/airports (REF-002 Phase 2).

Pure move from :mod:`backend.airports.router` — identical behavior, no
functional change. This module owns airport resolution (OurAirports),
the Overpass ``around`` fetch, and the radius math; the router keeps the
endpoints and the render pipeline.
"""

from __future__ import annotations

import json
import math

from fastapi import Request
from fastapi.responses import JSONResponse

from backend.airports import cache as cache_mod
from backend.airports.cache import cache_get, cache_set
from backend.airports.config import settings
from backend.airports.geometry import project
from backend.airports.ourairports import (
    AirportNotFoundError,
    OurAirportsError,
    airport_frequencies,
    airport_runways,
    heading_from_ident,
    resolve_airport,
    runway_endpoints,
)
from backend.airports.overpass import (
    AEROWAY_CLASSES,
    AirportOverpassError,
    build_context_query,
    fetch_airport_polygons,
)
from backend.airports.sources import (
    SOURCES_VERSION,
    AirfieldResult,
    is_degraded,
    load_airfield,
)
from backend.cancel import race_cancel
from backend.http import error_response, fnum


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
        return error_response(404, "airport_not_found", f"No airport found for ICAO '{icao}'.")
    except OurAirportsError as exc:
        return error_response(502, "ourairports_unavailable", f"Airport data fetch failed: {exc.detail}")
    cache_hit = bool(hit_airport and hit_rwy and hit_freq)
    if cache_hit:
        warnings.append("ourairports_cache_hit")
    return airport, runway_rows, freq_rows, warnings, cache_hit


def _runway_infos(runway_rows: list[dict]) -> list[dict]:
    infos = []
    for row in runway_rows:
        le_ident = (row.get("le_ident") or "").strip()
        he_ident = (row.get("he_ident") or "").strip()
        infos.append(
            {
                "le_ident": le_ident,
                "he_ident": he_ident,
                "length_ft": fnum(row.get("length_ft")),
                "width_ft": fnum(row.get("width_ft")),
                "surface": (row.get("surface") or "").strip(),
                "le_heading_deg": fnum(row.get("le_heading_degT"))
                or heading_from_ident(le_ident),
                "he_heading_deg": fnum(row.get("he_heading_degT"))
                or heading_from_ident(he_ident),
                "endpoints_derived": False,
            }
        )
    return infos


async def _load_polygons(
    lat: float, lon: float, radius_m: float, warnings: list[str],
    kind: str = "overpass-ctx",
    request: Request | None = None,
    endpoint: str = "/v1/airports/render",
    started_mono: float = 0.0,
) -> tuple[list[dict], None]:
    """Redis-first Overpass fetch of the surrounding street context
    (``build_context_query``). The airfield core never comes through here;
    it is :func:`_load_airfield`. ``kind`` namespaces the cache key. Context
    outages degrade to an empty layer (warning) instead of failing the render.

    P2: the fetch is raced against disconnect (no partial cache on abort).
    """
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
    query = build_context_query(lat, lon, radius_m)
    try:
        elements = await race_cancel(
            request, fetch_airport_polygons(query),
            endpoint=endpoint, stage="overpass", started_mono=started_mono,
        )
    except AirportOverpassError:
        warnings.append("context_unavailable")
        return [], None
    await cache_set(key, json.dumps(elements))
    return elements, None


#: Version tag of the cached airfield payload. Bump when AirfieldResult or
#: its filters change, so stale entries are never read back.
AIRFIELD_CACHE_VERSION = "airfield-v1"


def _airfield_cache_key(lat: float, lon: float, radius_m: float) -> str:
    """Key includes the pinned Overture release (or ``off``) and the source
    logic version, so a release bump or a toggle re-keys every airfield and
    never mixes two releases."""
    release = settings.overture_release if settings.overture_enabled else "off"
    return cache_mod.airports_cache_key(
        "airfield", f"{lat:.5f},{lon:.5f}", f"r={radius_m:.0f}",
        f"overture={release}", f"sources={SOURCES_VERSION}",
        AIRFIELD_CACHE_VERSION,
    )


def _airfield_to_json(result: AirfieldResult) -> str:
    return json.dumps({
        "geoms": {
            cls: [[list(p) for p in ln] for ln in lines]
            for cls, lines in result.geoms.items()
        },
        "taxiway_refs": [
            [ref, [list(p) for p in ln]] for ref, ln in result.taxiway_refs
        ],
        "sources": result.sources,
        "warnings": result.warnings,
        "overture_release": result.overture_release,
    }, separators=(",", ":"))


def _airfield_from_json(raw: str) -> AirfieldResult:
    """Inverse of :func:`_airfield_to_json`; tuples restored so a cache hit
    compares equal to the fresh result. Raises on a malformed payload."""
    data = json.loads(raw)
    geoms = {
        cls: [[tuple(p) for p in ln] for ln in data["geoms"].get(cls, [])]
        for cls in AEROWAY_CLASSES
    }
    return AirfieldResult(
        geoms=geoms,
        taxiway_refs=[
            (ref, [tuple(p) for p in ln]) for ref, ln in data["taxiway_refs"]
        ],
        sources=dict(data["sources"]),
        warnings=list(data["warnings"]),
        overture_release=data["overture_release"],
    )


async def _load_airfield(
    lat: float, lon: float, radius_m: float, warnings: list[str],
    request: Request | None = None,
    endpoint: str = "/v1/airports/render",
    started_mono: float = 0.0,
) -> tuple[AirfieldResult | None, JSONResponse | None]:
    """Redis-first airfield geometry via :func:`sources.load_airfield`.

    Returns ``(result, None)`` or ``(None, error response)`` when the full
    Overpass fallback fails too. A degraded result (``is_degraded``:
    ``osm_layers_unavailable`` or ``overture_unavailable``) is served once and
    never cached, so the next request retries the missing sources. An
    ``overture_empty`` fallback is complete Overpass data and is cached.
    ``result.warnings`` are appended to ``warnings``.

    P2: the load is raced against disconnect (no partial cache on abort).
    """
    key = _airfield_cache_key(lat, lon, radius_m)
    raw_json = await cache_get(key)
    if raw_json is not None:
        try:
            result = _airfield_from_json(raw_json)
        except (ValueError, KeyError, TypeError):
            result = None
        if result is not None:
            warnings.append("airfield_cache_hit")
            warnings.extend(result.warnings)
            return result, None
    try:
        result = await race_cancel(
            request, load_airfield(lat, lon, radius_m),
            endpoint=endpoint, stage="overpass", started_mono=started_mono,
        )
    except AirportOverpassError as exc:
        return None, error_response(
            502,
            "overpass_unavailable",
            f"Ground-layout fetch failed ({exc.detail}). "
            "(Upstreams busy — retry in a minute.)",
        )
    warnings.extend(result.warnings)
    if not is_degraded(result.warnings):
        await cache_set(key, _airfield_to_json(result))
    return result, None


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
    # Broad catches are deliberate best-effort fallbacks (moved verbatim):
    # one malformed runway row must not fail the radius, and any surprise
    # degrades to the requested radius instead of failing the render.
    try:
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


__all__ = [
    "_bucket_radius_m",
    "_effective_radius_m",
    "_load_airfield",
    "_load_polygons",
    "_lookup",
    "_runway_infos",
]
