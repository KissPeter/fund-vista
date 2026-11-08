"""Airfield geometry source for airport diagrams and traffic (F-008).

This module is the only place that decides where airfield geometry comes
from. The airfield core (runways, taxiways, aprons, stopways and taxiway
labels) comes from Overture Maps ``base/infrastructure``, which is a monthly
copy of the same OpenStreetMap ways. Terminals, hangars and stands are not in
Overture, so they come from a reduced Overpass query. When Overture fails (or
returns no rows) the full Overpass airfield query is used instead, so the
result is never worse than the Overpass-only path.

Both sources go through the same filters: a feature is kept when its nearest
point is within the radius (a circle, not a bbox) or, for a closed ring, when
the centre lies inside it; runways shorter than ``MIN_RUNWAY_M`` are dropped.

Degraded results (see ``DEGRADED_WARNINGS``) are served once and MUST NOT be
cached by the callers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field

import duckdb
import httpx

from backend.airports.config import settings
from backend.airports.overpass import (
    AEROWAY_CLASSES,
    AirportOverpassError,
    build_airport_query,
    extract_taxiway_refs,
    fetch_airport_polygons,
    split_aeroway,
)

log = logging.getLogger(__name__)

# Closed stubs such as LHBP runway 03/21 are shorter than this and draw as noise.
MIN_RUNWAY_M = 50.0

#: Version of the source logic (filters, assembly, fallback rules). Bump it
#: when any of them changes so cached SVGs and airfield payloads are re-keyed.
SOURCES_VERSION = "1"

#: Warnings that make a result degraded: it is served, never cached.
DEGRADED_WARNINGS = frozenset({"osm_layers_unavailable", "overture_unavailable"})

#: After an Overture read fails, Overture is skipped for this long, so an
#: outage does not cost a full timeout on every request.
OVERTURE_BACKOFF_S = 60.0

#: Overture's STAC catalog names the newest release in its ``latest`` field.
STAC_CATALOG_URL = "https://stac.overturemaps.org/catalog.json"
STAC_TIMEOUT_S = 5.0

# Overture ``class`` values mapped onto the Overpass class keys.
_OVERTURE_CLASS = {
    "runway": "runway",
    "taxiway": "taxiway",
    "taxilane": "taxiway",
    "apron": "apron",
    "stopway": "stopways",
}
_OVERTURE_LAYERS = ("runway", "taxiway", "apron", "stopways")
_OSM_LAYERS = ("terminal", "hangar", "stands")

_OVERTURE_BASE = "s3://overturemaps-us-west-2/release"
_RELEASE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+")

Line = list[tuple[float, float]]

# Module state for the Overture outage window and the STAC lookup. Tests use
# ``reset_overture_state`` to clear both.
_overture_down_until: float = 0.0
_latest_release: str | None = None


@dataclass
class AirfieldResult:
    """Airfield geometry for one centre point and radius.

    ``geoms`` always holds every key in ``AEROWAY_CLASSES``. ``sources`` has
    an entry only for the classes that were loaded, with ``"overture"`` or
    ``"osm"``; a class omitted after a reduced-Overpass failure has an empty
    list and no entry. ``warnings`` holds ``overture_unavailable``,
    ``overture_empty`` and/or ``osm_layers_unavailable``.
    ``overture_release`` is the release that was read (the pinned one, or the
    STAC latest after a 404) when Overture contributed, otherwise None.
    """

    geoms: dict[str, list[Line]] = field(
        default_factory=lambda: {cls: [] for cls in AEROWAY_CLASSES}
    )
    taxiway_refs: list[tuple[str, Line]] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    overture_release: str | None = None


def is_degraded(warnings) -> bool:
    """True when the warnings mark an airfield result as degraded (no cache)."""
    return any(w in DEGRADED_WARNINGS for w in warnings)


def reset_overture_state() -> None:
    """Clear the Overture outage window and the resolved STAC release."""
    global _overture_down_until, _latest_release
    _overture_down_until = 0.0
    _latest_release = None


def build_reduced_airport_query(lat: float, lon: float, radius_m: float) -> str:
    """Overpass QL for the layers Overture lacks: stands, hangars, terminals."""
    r = int(radius_m)
    at = f"(around:{r},{lat:.6f},{lon:.6f})"
    sel = '["aeroway"~"^(parking_position|hangar|terminal)$"]'
    return (
        "[out:json][timeout:25];\n"
        "(\n"
        f"  way{sel}{at};\n"
        f"  relation{sel}{at};\n"
        ");\n"
        "out geom;"
    )


def build_overture_sql(lat: float, lon: float, radius_m: float, release: str) -> str:
    """SQL that reads the airfield classes of one Overture release in the bbox.

    The release MUST fully match ``_RELEASE_RE``: it is interpolated into the
    S3 path, so anything else (quotes, whitespace, a trailing newline) is
    refused before a query is built. The bbox is pushed down to the parquet
    ``bbox`` struct; the circle filter is applied later.
    """
    if not isinstance(release, str) or not _RELEASE_RE.fullmatch(release):
        raise ValueError(f"invalid Overture release: {release!r}")
    west, south, east, north = _bbox(lat, lon, radius_m)
    path = f"{_OVERTURE_BASE}/{release}/theme=base/type=infrastructure/*"
    classes = ", ".join(f"'{c}'" for c in _OVERTURE_CLASS)
    return f"""
            select class,
                   json_extract_string(to_json(source_tags), '$.ref'),
                   ST_AsGeoJSON(geometry)
            from read_parquet('{path}', hive_partitioning=1)
            where bbox.xmin <= {east:.7f} and bbox.xmax >= {west:.7f}
              and bbox.ymin <= {north:.7f} and bbox.ymax >= {south:.7f}
              and subtype = 'airport' and class in ({classes})
            order by id
            """


def _ensure_extension(con: duckdb.DuckDBPyConnection, name: str) -> None:
    """LOAD a bundled extension; INSTALL it only when LOAD fails."""
    try:
        con.execute(f"LOAD {name}")
    except duckdb.Error:
        con.execute(f"INSTALL {name}")
        con.execute(f"LOAD {name}")


def read_overture_airfield(
    lat: float, lon: float, radius_m: float, release: str
) -> list[dict]:
    """Blocking read of Overture airport infrastructure inside the bbox.

    Returns raw rows ``{"class", "ref", "geometry"}`` where ``geometry`` is a
    GeoJSON dict. Runs in a worker thread (see ``_read_release``).
    """
    sql = build_overture_sql(lat, lon, radius_m, release)
    con = duckdb.connect()
    try:
        _ensure_extension(con, "spatial")
        _ensure_extension(con, "httpfs")
        con.execute("SET s3_region='us-west-2';")
        rows = con.execute(sql).fetchall()
    finally:
        con.close()
    return [
        {"class": cls, "ref": ref, "geometry": json.loads(gj)}
        for cls, ref, gj in rows
    ]


def _bbox(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """West, south, east, north of the circle's bounding box in degrees."""
    dlat = radius_m / 110540.0
    dlon = radius_m / (111320.0 * math.cos(math.radians(lat)))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def _xy(point: tuple[float, float], lat: float, lon: float) -> tuple[float, float]:
    """Local metres east and north of the centre (equirectangular)."""
    mlat = 110540.0
    mlon = 111320.0 * math.cos(math.radians(lat))
    return (point[0] - lon) * mlon, (point[1] - lat) * mlat


def _segment_distance_to_origin(
    a: tuple[float, float], b: tuple[float, float]
) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    seg_len2 = dx * dx + dy * dy
    t = 0.0 if seg_len2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg_len2))
    return math.hypot(ax + t * dx, ay + t * dy)


def _point_in_ring(pts: list[tuple[float, float]]) -> bool:
    """Whether the origin lies inside a closed ring in local metres (ray cast)."""
    inside = False
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        if (y1 > 0) != (y2 > 0):
            x_cross = x1 + (0 - y1) * (x2 - x1) / (y2 - y1)
            if x_cross > 0:
                inside = not inside
    return inside


def _is_closed(line: Line) -> bool:
    return len(line) >= 4 and tuple(line[0]) == tuple(line[-1])


def _keep_line(cls: str, line: Line, lat: float, lon: float, radius_m: float) -> bool:
    """Shared filter: circle test on the nearest point, and the runway floor.

    A closed ring (polygon) that encloses the centre is kept even when none of
    its edges is within the radius.
    """
    pts = [_xy(p, lat, lon) for p in line]
    if not pts:
        return False
    if len(pts) == 1:
        nearest = math.hypot(*pts[0])
    else:
        nearest = min(
            _segment_distance_to_origin(pts[i], pts[i + 1])
            for i in range(len(pts) - 1)
        )
    if nearest > radius_m and not (_is_closed(line) and _point_in_ring(pts)):
        return False
    if cls == "runway":
        length = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        if length < MIN_RUNWAY_M:
            return False
    return True


def _apply_shared_filters(
    geoms: dict[str, list[Line]],
    refs: list[tuple[str, Line]],
    lat: float,
    lon: float,
    radius_m: float,
) -> tuple[dict[str, list[Line]], list[tuple[str, Line]]]:
    """One filter for both sources: layers and taxiway labels alike."""
    kept = {
        cls: [ln for ln in lines if _keep_line(cls, ln, lat, lon, radius_m)]
        for cls, lines in geoms.items()
    }
    kept_refs = [
        (ref, ln) for ref, ln in refs if _keep_line("taxiway", ln, lat, lon, radius_m)
    ]
    return kept, kept_refs


def _geojson_lines(geom: dict) -> list[Line]:
    kind = geom.get("type")
    coords = geom.get("coordinates") or []
    if kind == "Point":
        return [[tuple(coords)]]
    if kind == "LineString":
        return [[tuple(p) for p in coords]]
    if kind == "MultiLineString":
        return [[tuple(p) for p in ln] for ln in coords]
    if kind == "Polygon":
        return [[tuple(p) for p in ring] for ring in coords]
    if kind == "MultiPolygon":
        return [[tuple(p) for p in ring] for poly in coords for ring in poly]
    return []


def _empty_geoms() -> dict[str, list[Line]]:
    return {cls: [] for cls in AEROWAY_CLASSES}


def _overture_layers(
    rows: list[dict],
) -> tuple[dict[str, list[Line]], list[tuple[str, Line]]]:
    geoms = _empty_geoms()
    refs: list[tuple[str, Line]] = []
    for row in rows:
        overture_cls = row.get("class")
        cls = _OVERTURE_CLASS.get(overture_cls)
        if cls is None:
            continue
        ref = (row.get("ref") or "").strip()
        for ln in _geojson_lines(row["geometry"]):
            geoms[cls].append(ln)
            # Labels only for taxiway, never taxilane (same rule as extract_taxiway_refs).
            if overture_cls == "taxiway" and ref:
                refs.append((ref, ln))
    return geoms, refs


def _full_result(
    elements: list[dict], lat: float, lon: float, radius_m: float,
    warnings: list[str],
) -> AirfieldResult:
    """Split, filter and assemble the full Overpass airfield. CPU only."""
    geoms, _ = split_aeroway(elements)
    geoms, refs = _apply_shared_filters(
        geoms, extract_taxiway_refs(elements), lat, lon, radius_m
    )
    return AirfieldResult(
        geoms=geoms,
        taxiway_refs=refs,
        sources={cls: "osm" for cls in AEROWAY_CLASSES},
        warnings=list(warnings),
    )


def _hybrid_result(
    rows: list[dict],
    osm_elements: list[dict] | None,
    lat: float,
    lon: float,
    radius_m: float,
    release: str,
    warnings: list[str],
) -> AirfieldResult:
    """Split, filter and assemble the hybrid result. CPU only."""
    warnings = list(warnings)
    ov_geoms, ov_refs = _overture_layers(rows)
    geoms = _empty_geoms()
    sources: dict[str, str] = {}
    for cls in _OVERTURE_LAYERS:
        geoms[cls] = ov_geoms[cls]
        sources[cls] = "overture"
    if osm_elements is None:
        warnings.append("osm_layers_unavailable")
    else:
        osm_geoms, _ = split_aeroway(osm_elements)
        for cls in _OSM_LAYERS:
            geoms[cls] = osm_geoms[cls]
            sources[cls] = "osm"

    geoms, refs = _apply_shared_filters(geoms, ov_refs, lat, lon, radius_m)
    return AirfieldResult(
        geoms=geoms,
        taxiway_refs=refs,
        sources=sources,
        warnings=warnings,
        overture_release=release,
    )


async def _read_release(
    lat: float, lon: float, radius_m: float, release: str
) -> list[dict]:
    return await asyncio.wait_for(
        asyncio.to_thread(read_overture_airfield, lat, lon, radius_m, release),
        timeout=settings.overture_timeout_s,
    )


#: An HTTP 404 ("HTTP 404", "HTTP/1.1 404 Not Found") or an S3 NoSuchKey. A
#: bare "not found" or a "404" inside another number does not count.
_HTTP_404_RE = re.compile(
    r"\bhttp(?:/[\d.]+)?[\s(:-]+(?:error\D{0,3})?\s*404\b|\bnosuchkey\b",
    re.IGNORECASE,
)
#: DuckDB's message for a release glob that matches no object (a missing release).
_EMPTY_GLOB = "no files found"


def _is_not_found(exc: Exception) -> bool:
    """True for a missing release: an HTTP 404 / NoSuchKey, or an empty S3 glob."""
    text = str(exc)
    return bool(_HTTP_404_RE.search(text)) or _EMPTY_GLOB in text.lower()


async def _latest_overture_release() -> str | None:
    """The STAC ``latest`` release, cached in a module variable; None on failure."""
    global _latest_release
    if _latest_release is not None:
        return _latest_release
    try:
        async with httpx.AsyncClient(
            timeout=STAC_TIMEOUT_S,
            headers={"User-Agent": settings.user_agent},
        ) as client:
            resp = await client.get(STAC_CATALOG_URL)
            resp.raise_for_status()
            latest = resp.json().get("latest")
    except Exception as exc:
        log.warning("Overture STAC catalog lookup failed: %s", exc)
        return None
    if not isinstance(latest, str) or not _RELEASE_RE.fullmatch(latest):
        log.warning("Overture STAC catalog gave no usable latest release")
        return None
    _latest_release = latest
    return latest


def _overture_failed(exc: Exception) -> tuple[None, None, str]:
    """Start the outage window and report the failure."""
    global _overture_down_until
    _overture_down_until = time.monotonic() + OVERTURE_BACKOFF_S
    log.warning("Overture airfield read failed: %s", exc)
    return None, None, "overture_unavailable"


async def _overture_rows(
    lat: float, lon: float, radius_m: float
) -> tuple[list[dict] | None, str | None, str | None]:
    """``(rows, release read, warning)``. ``rows`` is None on any failure.

    A failed read starts the outage window. A pinned release that 404s is
    retried once with the STAC latest release; if that fails too, the read
    counts as a failure. An empty result is ``overture_empty`` and does not
    start the window.
    """
    global _overture_down_until, _latest_release
    if time.monotonic() < _overture_down_until:
        return None, None, "overture_unavailable"
    release = settings.overture_release
    try:
        rows = await _read_release(lat, lon, radius_m, release)
    except Exception as exc:
        if not _is_not_found(exc):
            return _overture_failed(exc)
        latest = await _latest_overture_release()
        if latest is None or latest == release:
            return _overture_failed(exc)
        log.info("Overture release %s not found; retrying with %s", release, latest)
        try:
            rows = await _read_release(lat, lon, radius_m, latest)
        except Exception as retry_exc:
            _latest_release = None
            return _overture_failed(retry_exc)
        release = latest
    if not rows:
        log.warning("Overture returned no airfield rows; using Overpass")
        return None, None, "overture_empty"
    return rows, release, None


async def _osm_reduced(
    lat: float, lon: float, radius_m: float, client: httpx.AsyncClient | None
) -> list[dict] | None:
    """Reduced Overpass elements, or None when every mirror failed."""
    try:
        return await fetch_airport_polygons(
            build_reduced_airport_query(lat, lon, radius_m), client=client
        )
    except AirportOverpassError as exc:
        log.warning("reduced Overpass airfield query failed: %s", exc)
        return None


async def _load_osm_full(
    lat: float,
    lon: float,
    radius_m: float,
    client: httpx.AsyncClient | None,
    warnings: list[str],
) -> AirfieldResult:
    """Today's full Overpass airfield query. Raises AirportOverpassError."""
    elements = await fetch_airport_polygons(
        build_airport_query(lat, lon, radius_m), client=client
    )
    return await asyncio.to_thread(
        _full_result, elements, lat, lon, radius_m, warnings
    )


async def load_airfield(
    lat: float,
    lon: float,
    radius_m: float,
    *,
    client: httpx.AsyncClient | None = None,
) -> AirfieldResult:
    """Airfield geometry around ``(lat, lon)``, hybrid Overture + Overpass.

    With ``settings.overture_enabled`` False, only the full Overpass path runs.
    While Overture is in its outage window the full Overpass path runs
    directly. Otherwise Overture and the reduced Overpass query run
    concurrently. An Overture failure or empty result MUST fall back to the
    full Overpass query. A reduced Overpass failure keeps the Overture layers.
    Raises AirportOverpassError only when the full Overpass query fails.
    Splitting and filtering run in a worker thread.
    """
    if not settings.overture_enabled:
        return await _load_osm_full(lat, lon, radius_m, client, [])
    if time.monotonic() < _overture_down_until:
        return await _load_osm_full(
            lat, lon, radius_m, client, ["overture_unavailable"]
        )

    (rows, release, problem), osm_elements = await asyncio.gather(
        _overture_rows(lat, lon, radius_m),
        _osm_reduced(lat, lon, radius_m, client),
    )
    if rows is None:
        return await _load_osm_full(lat, lon, radius_m, client, [problem])

    return await asyncio.to_thread(
        _hybrid_result, rows, osm_elements, lat, lon, radius_m, release, []
    )


__all__ = [
    "DEGRADED_WARNINGS",
    "MIN_RUNWAY_M",
    "SOURCES_VERSION",
    "AirfieldResult",
    "build_overture_sql",
    "build_reduced_airport_query",
    "is_degraded",
    "load_airfield",
    "read_overture_airfield",
    "reset_overture_state",
]
