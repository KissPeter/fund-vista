"""Extract coverage: is a bbox entirely inside the area the extract holds?

Geofabrik extracts are clipped by a polygon (``hungary.poly``), not a
rectangle, so a rectangle test would answer border bboxes (Bratislava,
Vienna) from an extract that holds none of their data. The test here is
exact for simple polygons: every bbox corner inside, and no polygon edge
touching the bbox.
"""

from __future__ import annotations

from pathlib import Path

BBox = tuple[float, float, float, float]  # south, west, north, east
Ring = list[tuple[float, float]]  # (lon, lat)


def parse_poly(text: str) -> list[dict]:
    """Parse an osmosis ``.poly`` file into ``[{"hole": bool, "pts": [[lon, lat]...]}]``."""
    rings: list[dict] = []
    cur: dict | None = None
    lines = [ln.strip() for ln in text.splitlines()]
    for ln in lines[1:]:  # first line is the area name
        if not ln:
            continue
        if ln == "END":
            if cur is None:
                break  # final END closes the file
            if len(cur["pts"]) >= 3:
                rings.append(cur)
            cur = None
            continue
        if cur is None:
            cur = {"hole": ln.startswith("!"), "pts": []}
            continue
        lon, lat = ln.split()[:2]
        cur["pts"].append([float(lon), float(lat)])
    if not rings:
        raise ValueError("no polygon rings found")
    return rings


def load_poly(path: Path) -> list[dict]:
    return parse_poly(path.read_text())


def poly_bounds(rings: list[dict]) -> BBox:
    lons = [p[0] for r in rings for p in r["pts"]]
    lats = [p[1] for r in rings for p in r["pts"]]
    return (min(lats), min(lons), max(lats), max(lons))


def _inside(rings: list[dict], lon: float, lat: float) -> bool:
    """Even-odd rule over all rings (holes toggle naturally)."""
    inside = False
    for ring in rings:
        pts = ring["pts"]
        j = len(pts) - 1
        for i in range(len(pts)):
            xi, yi = pts[i]
            xj, yj = pts[j]
            if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def _seg_hits_rect(
    ax: float, ay: float, bx: float, by: float,
    x0: float, y0: float, x1: float, y1: float,
) -> bool:
    """Whether segment A-B touches the closed rectangle (Liang-Barsky)."""
    if max(ax, bx) < x0 or min(ax, bx) > x1 or max(ay, by) < y0 or min(ay, by) > y1:
        return False
    dx, dy = bx - ax, by - ay
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return False
            t0 = max(t0, t)
        else:
            if t < t0:
                return False
            t1 = min(t1, t)
    return t0 <= t1


def covers(rings: list[dict], bbox: BBox) -> bool:
    south, west, north, east = bbox
    for lon, lat in ((west, south), (east, south), (east, north), (west, north)):
        if not _inside(rings, lon, lat):
            return False
    for ring in rings:
        pts = ring["pts"]
        for i in range(len(pts)):
            ax, ay = pts[i]
            bx, by = pts[(i + 1) % len(pts)]
            if _seg_hits_rect(ax, ay, bx, by, west, south, east, north):
                return False
    return True


def clip_bbox(rings: list[dict], bbox: BBox) -> BBox | None:
    """Intersect ``bbox`` with the coverage rectangle, or ``None`` if disjoint.

    Unlike :func:`covers`, this never requires the whole request to fit: an
    oversized view (a traffic day-slice can be far larger than Hungary) is
    answered from the part the extract holds. The result is a rectangle, so
    callers must treat it as complete only inside the covered area; touching
    the rectangle at an edge only counts as an empty intersection.
    """
    if not rings:
        return None
    south, west, north, east = bbox
    bs, bw, bn, be = poly_bounds(rings)
    clipped = (max(south, bs), max(west, bw), min(north, bn), min(east, be))
    s, w, n, e = clipped
    return clipped if s < n and w < e else None


def rect_ring(bbox: BBox) -> list[dict]:
    """A rectangle as coverage (when no ``.poly`` is supplied)."""
    s, w, n, e = bbox
    return [{"hole": False, "pts": [[w, s], [e, s], [e, n], [w, n]]}]
