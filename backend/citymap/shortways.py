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


def drop_short_ways(elements: list[dict], min_m: float) -> list[dict]:
    """Elements without ways shorter than ``min_m`` and without their orphaned nodes.

    Relations are kept (their member ways may now be absent, which the
    splitter already tolerates). A way whose node coordinates are not all
    available is kept: when in doubt, keep.
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
