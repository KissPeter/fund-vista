"""Axis-aligned knock-out: remove the part of every stroke inside a rectangle.

The stats table is a cartouche: everything inside its box (and the inset
strip between the box and the page corner) must be blank paper, otherwise the
map's streets show through the table text and borders. Strokes crossing the
rectangle are split at its edge; the parts outside are kept untouched.

Pure geometry in page-millimetre space, no dependencies.
"""

from __future__ import annotations

from backend.penplot.methods import Polyline

Rect = tuple[float, float, float, float]  # x_min, y_min, x_max, y_max

_EPS = 1e-9


def _inside_interval(
    p: tuple[float, float], q: tuple[float, float], rect: Rect
) -> tuple[float, float] | None:
    """Liang–Barsky: parameter interval ``[t0, t1]`` of segment p→q inside ``rect``."""
    x_min, y_min, x_max, y_max = rect
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for d, lo in ((-dx, p[0] - x_min), (dx, x_max - p[0]), (-dy, p[1] - y_min), (dy, y_max - p[1])):
        if abs(d) < _EPS:
            if lo < 0:
                return None  # parallel to this edge and outside it
            continue
        t = lo / d
        if d < 0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    return (t0, t1) if t0 < t1 else None


def _lerp(p: tuple[float, float], q: tuple[float, float], t: float) -> tuple[float, float]:
    return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)


def _outside_pieces(
    p: tuple[float, float], q: tuple[float, float], rect: Rect
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    inside = _inside_interval(p, q, rect)
    if inside is None:
        return [(p, q)]
    t0, t1 = inside
    pieces = []
    if t0 > _EPS:
        pieces.append((p, _lerp(p, q, t0)))
    if t1 < 1.0 - _EPS:
        pieces.append((_lerp(p, q, t1), q))
    return pieces


def knock_out(lines: list[Polyline], rect: Rect) -> list[Polyline]:
    """Return ``lines`` with everything strictly inside ``rect`` removed."""
    out: list[Polyline] = []
    for pl in lines:
        if len(pl) < 2:
            continue
        current: Polyline = []
        for a, b in zip(pl, pl[1:]):
            for pa, pb in _outside_pieces(a, b, rect):
                if current and abs(current[-1][0] - pa[0]) < 1e-6 and abs(current[-1][1] - pa[1]) < 1e-6:
                    current.append(pb)
                else:
                    if len(current) >= 2:
                        out.append(current)
                    current = [pa, pb]
        if len(current) >= 2:
            out.append(current)
    return out
