"""OSM basemap for the traffic map: motorways, waterways, lakes, airfield.

Port of the ``tools/airport_traffic/basemap.py`` POC queries and geometry
parsing; the POC disk cache is replaced by the Redis L3 layer
(``traffic:v1:osm:{layer}:{sha1(quantised bbox)}``, plan §6.3) and the fetch
goes through the shared Overpass mirror-failover layer
(:func:`backend.airports.overpass.fetch_airport_polygons`).

Outage policy (plan §6.3): per-layer failures degrade to an empty layer —
the caller records which layers failed and answers with a
``basemap_unavailable`` warning. A render never fails for a dead basemap.
"""

from __future__ import annotations

import json

from backend.airports.overpass import (
    AirportOverpassError,
    build_airport_query,
    fetch_airport_polygons,
    split_aeroway,
)
from backend.traffic import cache as _cache

LAYERS = ("motorways", "waterways", "water", "airfield")
BBox = tuple[float, float, float, float]  # south, north, west, east

#: Quantisation for the L3 key + query: neighbours within ~100 m share one
#: Overpass fetch instead of fragmenting the cache per pan pixel.
SNAP_DEG = 0.001

#: `around` radius for the airfield query (POC value — covers runways,
#: taxiways, aprons and terminals for most fields).
AIRFIELD_RADIUS_M = 6000


def quantise(b: BBox) -> BBox:
    """Snap a view bbox outward to the L3 grid (stable keys for near-identical pans)."""
    s, n, w, e = b
    q = SNAP_DEG
    import math as _math

    return (_math.floor(s / q) * q, _math.ceil(n / q) * q,
            _math.floor(w / q) * q, _math.ceil(e / q) * q)


def _bb(b: BBox) -> str:
    s, n, w, e = b
    return "%.5f,%.5f,%.5f,%.5f" % (s, w, n, e)


def motorway_query(b: BBox) -> str:
    return '[out:json][timeout:90];\nway["highway"="motorway"](%s);\nout geom;' % _bb(b)


def waterway_query(b: BBox) -> str:
    return '[out:json][timeout:90];\nway["waterway"="river"](%s);\nout geom;' % _bb(b)


def water_query(b: BBox) -> str:
    sel = '["natural"="water"]["name"]["water"~"^(lake|reservoir)$"](%s)' % _bb(b)
    return '[out:json][timeout:90];\n(\n  way%s;\n  relation%s;\n);\nout geom;' % (sel, sel)


def polylines(elements: list[dict]) -> list[list[list[float]]]:
    """``out geom`` ways + relation members -> [[lon, lat], ...] polylines."""
    out: list[list[list[float]]] = []

    def add(geom: list[dict] | None) -> None:
        pts = [[g["lon"], g["lat"]] for g in geom or [] if g.get("lat") is not None and g.get("lon") is not None]
        if len(pts) >= 2:
            out.append(pts)

    for el in elements:
        if el.get("type") == "way":
            add(el.get("geometry"))
        elif el.get("type") == "relation":
            for m in el.get("members", []) or []:
                if m.get("type") == "way":
                    add(m.get("geometry"))
    return out


def layer_key(layer: str, view: BBox) -> str:
    """L3 cache key for one basemap layer over a quantised view."""
    return _cache.traffic_cache_key("osm", layer, "%.3f,%.3f,%.3f,%.3f" % quantise(view))


async def _fetch_layer(layer: str, view: BBox, lat: float, lon: float) -> list:
    if layer == "motorways":
        return polylines(await fetch_airport_polygons(motorway_query(view)))
    if layer == "waterways":
        return polylines(await fetch_airport_polygons(waterway_query(view)))
    if layer == "water":
        return polylines(await fetch_airport_polygons(water_query(view)))
    geoms, _counts = split_aeroway(
        await fetch_airport_polygons(build_airport_query(lat, lon, AIRFIELD_RADIUS_M))
    )
    # runways/taxiways/aprons/terminals only (stands, hangars and stopways
    # are poster clutter at traffic-map scale — POC decision, kept).
    return [[list(p) for p in ring] for cls in ("runway", "taxiway", "apron", "terminal")
            for ring in geoms.get(cls, [])]


async def load_basemap(view: BBox, lat: float, lon: float,
                       layers: tuple[str, ...] = LAYERS) -> tuple[dict[str, list], list[str]]:
    """Cached basemap polylines + the list of layers that failed.

    L3-first per layer; misses fetch from Overpass (mirror failover inside)
    and populate the cache. A failed layer comes back empty and is named in
    ``failed`` — the caller warns, never fails the render.
    """
    base: dict[str, list] = {k: [] for k in LAYERS}
    failed: list[str] = []
    wanted = [k for k in LAYERS if k in layers]
    keys = {k: layer_key(k, view) for k in wanted}
    found = await _cache.cache_get_many(list(keys.values()))
    todo: dict[str, str] = {}
    for k in wanted:
        raw = found.get(keys[k])
        if raw is not None:
            try:
                base[k] = json.loads(raw)
                continue
            except ValueError:
                pass
        todo[k] = keys[k]
    for k, key in todo.items():
        try:
            lines = await _fetch_layer(k, quantise(view), lat, lon)
        except AirportOverpassError:
            failed.append(k)
            continue
        except Exception:
            failed.append(k)
            continue
        base[k] = lines
        try:
            await _cache.cache_set(key, json.dumps(lines, separators=(",", ":")))
        except Exception:
            pass
    return base, failed


__all__ = [
    "AIRFIELD_RADIUS_M",
    "LAYERS",
    "SNAP_DEG",
    "layer_key",
    "load_basemap",
    "motorway_query",
    "polylines",
    "quantise",
    "water_query",
    "waterway_query",
]
