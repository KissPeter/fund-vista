"""Small geodesy helpers (equirectangular; good enough at airport/country scale).

Pure port of the ``tools/airport_traffic/geo.py`` POC — logic unchanged so
classifier behaviour stays identical between the POC CLI and the backend.
"""

from __future__ import annotations

import math

KM_PER_DEG = 111.2


def km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat1 - lat2) * KM_PER_DEG
    dx = (lon1 - lon2) * KM_PER_DEG * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


def bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Initial bearing a->b in degrees; points are (lat, lon)."""
    dy = b[0] - a[0]
    dx = (b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.degrees(math.atan2(dx, dy)) % 360


def rdp(pts: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    """Ramer-Douglas-Peucker (iterative, so long traces cannot hit the recursion limit)."""
    n = len(pts)
    if n < 3:
        return list(pts)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = pts[i], pts[j]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy) or 1e-12
        dmax, idx = 0.0, -1
        for k in range(i + 1, j):
            d = abs(dy * pts[k][0] - dx * pts[k][1] + x2 * y1 - y2 * x1) / norm
            if d > dmax:
                dmax, idx = d, k
        if dmax > eps and idx > 0:
            keep[idx] = True
            stack.extend(((i, idx), (idx, j)))
    return [p for p, k in zip(pts, keep) if k]


def clip_polyline(line: list[tuple[float, float]], w: float, h: float) -> list[list[tuple[float, float]]]:
    """Clip a polyline to [0,w]x[0,h] (Liang-Barsky per segment); returns the visible pieces."""
    out: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    for (x0, y0), (x1, y1) in zip(line, line[1:]):
        t0, t1, dx, dy = 0.0, 1.0, x1 - x0, y1 - y0
        visible = True
        for p, q in ((-dx, x0), (dx, w - x0), (-dy, y0), (dy, h - y0)):
            if p == 0:
                if q < 0:
                    visible = False
                    break
            else:
                r = q / p
                if p < 0:
                    t0 = max(t0, r)
                else:
                    t1 = min(t1, r)
        if not visible or t0 > t1:
            if len(cur) > 1:
                out.append(cur)
            cur = []
            continue
        a = (x0 + t0 * dx, y0 + t0 * dy)
        b = (x0 + t1 * dx, y0 + t1 * dy)
        if not cur or math.hypot(cur[-1][0] - a[0], cur[-1][1] - a[1]) > 1e-6:
            if len(cur) > 1:
                out.append(cur)
            cur = [a]
        cur.append(b)
        if t1 < 1:
            out.append(cur)
            cur = []
    if len(cur) > 1:
        out.append(cur)
    return out
