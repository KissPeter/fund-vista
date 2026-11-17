"""Early length filter: drop ways the renderer would drop anyway.

``render_svg`` drops polylines shorter than ``min_path_len_m`` at the very
end, after every way has been fetched, held, split and projected. Dropping
them where elements enter our code cuts the cached tile size and the memory
held through the split and SVG build.

Soundness rule: the early filter must never remove a way the renderer would
keep. So the user's value is floored to a *bucket* (largest bucket not above
it), the threshold is the bucket minus a margin (the renderer measures in a
bbox-centred projection, this module per segment), and the bucket joins every
cache key an early-filtered payload is stored under. The exact filter in
``render_svg`` still runs last, so over-inclusion is harmless.
"""

from __future__ import annotations

import math

# Floor targets for ``min_path_len_m`` (0 = no early filter). The default
# slider value (10 m) is a bucket on purpose.
BUCKETS: tuple[float, ...] = (0.0, 5.0, 10.0, 25.0, 50.0, 100.0)
# Projection differences are far below this; it only has to stay safe.
MARGIN = 0.95

_M_PER_DEG_LAT = 110540.0
_M_PER_DEG_LON_EQUATOR = 111320.0


def detail_bucket(min_path_len_m: float) -> float:
    """Largest bucket not above ``min_path_len_m`` (0 when below the first)."""
    best = 0.0
    for b in BUCKETS:
        if b <= min_path_len_m:
            best = b
    return best


def bucket_key(bucket: float) -> str:
    return f"m{bucket:g}"


def threshold_m(min_path_len_m: float) -> float:
    """Length below which a way may safely be dropped early."""
    return detail_bucket(min_path_len_m) * MARGIN


def polyline_length_m(latlon: list[tuple[float, float]]) -> float:
    total = 0.0
    for i in range(1, len(latlon)):
        la0, lo0 = latlon[i - 1]
        la1, lo1 = latlon[i]
        cos_lat = math.cos(math.radians((la0 + la1) / 2.0))
        dx = (lo1 - lo0) * _M_PER_DEG_LON_EQUATOR * cos_lat
        dy = (la1 - la0) * _M_PER_DEG_LAT
        total += math.hypot(dx, dy)
    return total


def g_length_m(g: list[float]) -> float:
    """Length of a compact way geometry ``[lon0, lat0, lon1, lat1, ...]``."""
    total = 0.0
    for i in range(2, len(g), 2):
        lo0, la0, lo1, la1 = g[i - 2], g[i - 1], g[i], g[i + 1]
        cos_lat = math.cos(math.radians((la0 + la1) / 2.0))
        dx = (lo1 - lo0) * _M_PER_DEG_LON_EQUATOR * cos_lat
        dy = (la1 - la0) * _M_PER_DEG_LAT
        total += math.hypot(dx, dy)
    return total


def compact_elements(elements: list[dict], min_m: float = 0.0) -> list[dict]:
    """Overpass node/way/relation elements -> compact elements (+ length filter).

    A compact way carries its geometry inline, ``g = [lon0, lat0, lon1,
    lat1, ...]``, and no ``nodes``: the node dicts (the bulk of an Overpass
    answer, ~300 B each in Python) disappear and a node is ~64 B in ``g``.
    Relations are kept as they are. Ways already compact pass through, so a
    mixed or repeated call is harmless. Ways shorter than ``min_m`` are
    dropped; a way with an unresolved node is kept whole-as-resolved (the
    splitter has always skipped missing coordinates) and never dropped on
    length alone.
    """
    coords: dict[int, tuple[float, float]] = {}
    for el in elements:
        if el.get("type") == "node" and "lat" in el and "lon" in el:
            coords[el["id"]] = (el["lon"], el["lat"])
    out: list[dict] = []
    for el in elements:
        kind = el.get("type")
        if kind == "way":
            if "g" in el:
                if min_m > 0 and g_length_m(el["g"]) < min_m and len(el["g"]) >= 4:
                    continue
                out.append(el)
                continue
            refs = el.get("nodes", [])
            g: list[float] = []
            for r in refs:
                c = coords.get(r)
                if c is not None:
                    g.append(c[0])
                    g.append(c[1])
            if min_m > 0 and len(g) == 2 * len(refs) and len(g) >= 4 and g_length_m(g) < min_m:
                continue
            out.append({"type": "way", "id": el["id"], "tags": el.get("tags") or {}, "g": g})
        elif kind == "relation":
            out.append(el)
    return out


def drop_short_ways(elements: list[dict], min_m: float) -> list[dict]:
    """Elements without ways shorter than ``min_m`` (node form: orphaned nodes go too).

    Compact ways are filtered by their inline geometry. Node-form input keeps
    the legacy shape: ways dropped, nodes no kept way references dropped,
    relations kept, and a way with unresolved nodes kept.
    """
    if min_m <= 0:
        return elements
    coords: dict[int, tuple[float, float]] = {}
    for el in elements:
        if el.get("type") == "node" and "lat" in el and "lon" in el:
            coords[el["id"]] = (el["lat"], el["lon"])
    kept: list[dict] = []
    used: set[int] = set()
    for el in elements:
        kind = el.get("type")
        if kind == "way":
            if "g" in el:
                if len(el["g"]) >= 4 and g_length_m(el["g"]) < min_m:
                    continue
                kept.append(el)
                continue
            refs = el.get("nodes", [])
            pts = [coords[r] for r in refs if r in coords]
            if len(pts) == len(refs) and len(pts) >= 2 and polyline_length_m(pts) < min_m:
                continue
            used.update(refs)
            kept.append(el)
        elif kind == "relation":
            kept.append(el)
    for el in elements:
        if el.get("type") == "node" and el["id"] in used:
            kept.append(el)
    return kept
