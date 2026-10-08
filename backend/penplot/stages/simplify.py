"""Simplification + smoothing stages (REF-002 Phase 4).

Pure move from :mod:`backend.penplot.optimize` — identical behavior:
Ramer–Douglas–Peucker (open + closed-ring), Chaikin corner-cutting,
and segment densification.
"""

from __future__ import annotations

import logging
import math

from backend.penplot.methods import Polyline
from backend.penplot.stages._grid import count_points, dist

log = logging.getLogger(__name__)


def _rdp_open(points: Polyline, eps: float) -> Polyline:
    """RDP core for open polylines (endpoints must differ)."""
    if len(points) < 3 or eps <= 0:
        return list(points)
    x0, y0 = points[0]
    x1, y1 = points[-1]
    dx, dy = x1 - x0, y1 - y0
    denom = math.hypot(dx, dy) or 1e-12
    best_i, best_d = -1, 0.0
    for i in range(1, len(points) - 1):
        px, py = points[i]
        d = abs(dy * px - dx * py + x1 * y0 - y1 * x0) / denom
        if d > best_d:
            best_d, best_i = d, i
    if best_d > eps:
        left = _rdp_open(points[: best_i + 1], eps)
        right = _rdp_open(points[best_i:], eps)
        return left[:-1] + right
    return [points[0], points[-1]]


def _rdp(points: Polyline, eps: float) -> Polyline:
    """RDP that also handles closed rings.

    A closed ring has a zero-length chord, so the open-polyline distance test
    degenerates (every distance reads as 0 and the ring collapses to two
    identical points). Break the ring open at the vertex farthest from vertex
    0 first — endpoints are then far apart and simplification is exact.
    """
    if len(points) < 3 or eps <= 0:
        return list(points)
    if dist(points[0], points[-1]) <= 1e-9:
        body = points[:-1]
        if len(body) < 3:
            return list(points)
        k = max(range(len(body)), key=lambda i: dist(body[i], body[0]))
        # Cut the ring at k into an OPEN chain (endpoints are ring-adjacent),
        # simplify the chain, then re-close it.
        chain = body[k:] + body[:k]
        simp = _rdp_open(chain, eps)
        if dist(simp[0], simp[-1]) > 1e-9:
            simp.append(simp[0])
        return simp
    return _rdp_open(points, eps)


def linesimplify(lines: list[Polyline], tol: float) -> list[Polyline]:
    if tol <= 0:
        return [list(pl) for pl in lines]
    out = [_rdp(pl, tol) for pl in lines]
    log.debug(
        "linesimplify: %d -> %d pts (tol=%.3fmm)",
        count_points(lines), count_points(out), tol,
    )
    return out


def _chaikin_once(points: Polyline) -> Polyline:
    if len(points) < 3:
        return list(points)
    closed = dist(points[0], points[-1]) <= 1e-9
    body = points[:-1] if closed else points
    if len(body) < 3:
        return list(points)
    out: Polyline = []
    n = len(body)
    for i in range(n if closed else n - 1):
        p0 = body[i]
        p1 = body[(i + 1) % n] if closed else body[i + 1]
        if i == 0 and not closed:
            out.append(p0)  # keep open endpoints fixed
        q = (0.75 * p0[0] + 0.25 * p1[0], 0.75 * p0[1] + 0.25 * p1[1])
        r = (0.25 * p0[0] + 0.75 * p1[0], 0.25 * p0[1] + 0.75 * p1[1])
        out += [q, r]
    if closed:
        out.append(out[0])
    else:
        out.append(body[-1])
    return out


def curvesmooth(lines: list[Polyline], iterations: int) -> list[Polyline]:
    """Round polyline corners via Chaikin corner-cutting (mm space)."""
    iters = max(0, int(iterations))
    if iters <= 0:
        return [list(pl) for pl in lines]
    out = [list(pl) for pl in lines]
    for _ in range(min(iters, 5)):  # CPU/size guard: each pass ~doubles points
        out = [_chaikin_once(pl) for pl in out]
    log.debug(
        "curvesmooth: %d -> %d pts (iters=%d)",
        count_points(lines), count_points(out), iters,
    )
    return out


def densify(lines: list[Polyline], max_seg: float) -> list[Polyline]:
    """Split segments longer than ``max_seg`` into equal collinear pieces.

    Chaikin removes a fixed fraction of each adjacent segment, so on a long,
    sparsely sampled polyline (a page frame, a ground line, a simplified long
    edge) corner cutting rounds corners into huge blobs. Capping segment
    length bounds the rounding radius and leaves straight runs straight.
    """
    out: list[Polyline] = []
    for line in lines:
        if not line:
            out.append(line)
            continue
        pts = [line[0]]
        for (x0, y0), (x1, y1) in zip(line, line[1:]):
            n = max(int(math.hypot(x1 - x0, y1 - y0) // max_seg) + 1, 1)
            pts.extend((x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n)
                       for k in range(1, n + 1))
        out.append(pts)
    return out


__all__ = ["curvesmooth", "densify", "linesimplify"]
