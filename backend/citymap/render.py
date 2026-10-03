"""Plane projection + plotter-friendly SVG rendering.

Lon/lat polylines are projected with an equirectangular approximation
around the bbox center (the same flat-plane idea as city-roads' Grid —
good to ~1% over a metro area) and drawn into a ``width``-wide canvas
framed on the *bbox* — never on the data extent — so enabling another
layer can add paths but never moves or rescales the ones already drawn
(the layer-toggle view-area bug). Each requested layer becomes one
``<g id="citymap-<layer>">`` of stroked paths; area layers (buildings,
water, aeroway) emit closed outlines, everything else open polylines.
Fills are always ``none`` so the output plots cleanly.

The output is chrome-free by construction: no location caption, no
attribution comment. (For SVGs exported by the upstream city-roads app,
which do carry those, see :mod:`backend.citymap.chrome`.) The ODbL credit
lives in the API response metadata instead (``attribution`` field).
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import math
import sys

from backend.citymap.overpass import BBox

# Layers whose closed rings are outlines of areas (emit a closing Z).
AREA_LAYERS = frozenset({"buildings", "water", "aeroway"})


@functools.lru_cache(maxsize=1)
def render_source_version() -> str:
    """Version of the rendered output, baked into the SVG cache key so
    clients never see art drawn under an older framing.

    Content hash of this file's own source (same convention as the
    airports renderer): EVERY code change busts the cache automatically,
    no manual bump to forget."""
    return hashlib.sha1(
        inspect.getsource(sys.modules[__name__]).encode("utf-8")
    ).hexdigest()[:12]

_M_PER_DEG_LAT = 110540.0
_M_PER_DEG_LON_EQUATOR = 111320.0


def project(
    lon: float, lat: float, lon0: float, lat0: float
) -> tuple[float, float]:
    """Equirectangular projection to plane meters around (lon0, lat0)."""
    x = (lon - lon0) * _M_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat0))
    y = (lat0 - lat) * _M_PER_DEG_LAT  # SVG y grows downwards = south
    return x, y


def _clip_segment(
    p0: tuple[float, float], p1: tuple[float, float],
    x0: float, y0: float, x1: float, y1: float,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Liang-Barsky clip of one segment to an axis-aligned rect (or None)."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, p0[0] - x0), (dx, x1 - p0[0]),
                 (-dy, p0[1] - y0), (dy, y1 - p0[1])):
        if abs(p) < 1e-12:
            if q < 0.0:
                return None
        else:
            r = q / p
            if p < 0.0:
                t0 = max(t0, r)
            else:
                t1 = min(t1, r)
            if t0 > t1:
                return None
    return ((p0[0] + t0 * dx, p0[1] + t0 * dy),
            (p0[0] + t1 * dx, p0[1] + t1 * dy))


def _clip_polyline(
    pts: list[tuple[float, float]],
    x0: float, y0: float, x1: float, y1: float,
) -> list[list[tuple[float, float]]]:
    """Clip a polyline to an axis-aligned rect, splitting into kept runs."""
    runs: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    for p0, p1 in zip(pts, pts[1:]):
        seg = _clip_segment(p0, p1, x0, y0, x1, y1)
        if seg is None:
            if cur:
                runs.append(cur)
                cur = []
            continue
        q0, q1 = seg
        if not cur:
            cur = [q0]
        if math.hypot(q1[0] - cur[-1][0], q1[1] - cur[-1][1]) > 1e-9:
            cur.append(q1)
    if cur:
        runs.append(cur)
    return [run for run in runs if len(run) > 1]


def render_svg(
    geoms: dict[str, list[list[tuple[float, float]]]],
    bbox: BBox,
    layers: list[str],
    *,
    width: int = 1000,
    min_path_len_m: float = 0.0,
    bearing_deg: float = 0.0,
    viewport_aspect: float | None = None,
    cancelled: object = None,
    cancel_every: int = 2000,
) -> tuple[str, dict[str, int]]:
    """Render layer geometries to a chrome-free SVG document.

    Returns ``(svg_text, path_counts)``. Polylines shorter than
    ``min_path_len_m`` meters are dropped (the city-roads ``minLength``
    pen-plotter option, but in meters instead of pixels).

    Framing contract: the canvas dimensions and the plane-meter → SVG
    mapping derive from the *bbox*, never from the data extent — the same
    bbox always yields the same frame and the same coordinates for the
    same geometry, whatever the layer set (a layer toggle only adds or
    removes paths). Content spilling past the frame — Overpass pulls whole
    ways, so edge ways overhang — is clipped to it (Liang-Barsky,
    splitting into kept runs).

    ``cancelled`` is an optional ``() -> bool`` polled every ``cancel_every``
    polylines (P2 checkpoint 4). Raises ``ClientCancelled`` when it fires.
    """
    south, west, north, east = bbox
    lon0, lat0 = (west + east) / 2.0, (south + north) / 2.0

    # Bearing clockwise from north (SVG y grows downwards = south, so a
    # screen-clockwise rotation is the standard matrix here). Rotated
    # content turns about the bbox center and clips to the framed viewport
    # rect recovered from the bbox + aspect; north-up clips to the bbox
    # rect itself. Both frames are content-independent (see above).
    bearing = bearing_deg % 360.0
    rotated = not (bearing < 1e-9 or bearing > 360.0 - 1e-9)
    if rotated and viewport_aspect is None:
        raise ValueError("'viewport_aspect' is required when 'bearing_deg' is non-zero.")
    theta = math.radians(bearing)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    span_x_m = (east - west) * _M_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat0))
    span_y_m = (north - south) * _M_PER_DEG_LAT
    if rotated:
        u, v = abs(cos_t), abs(sin_t)
        a = viewport_aspect or 1.0
        view_h = 0.5 * (span_x_m / (a * u + v) + span_y_m / (a * v + u))
        view_w = a * view_h
        fx0, fy0, fx1, fy1 = -view_w / 2.0, -view_h / 2.0, view_w / 2.0, view_h / 2.0
    else:
        # project() centers on the bbox midpoint, so the bbox rect is
        # symmetric about the origin.
        fx0, fy0, fx1, fy1 = (
            -span_x_m / 2.0, -span_y_m / 2.0, span_x_m / 2.0, span_y_m / 2.0,
        )

    def _is_cancelled() -> bool:
        try:
            return bool(callable(cancelled) and cancelled())  # type: ignore[operator]
        except Exception:
            return False

    projected: dict[str, list[list[tuple[float, float]]]] = {}
    seen = 0
    for layer in layers:
        polys: list[list[tuple[float, float]]] = []
        for lonlat in geoms.get(layer, []):
            seen += 1
            if cancelled is not None and seen % max(1, cancel_every) == 0 and _is_cancelled():
                from backend.cancel import ClientCancelled

                raise ClientCancelled("citymap", "svg_build")
            pts = [project(lon, lat, lon0, lat0) for lon, lat in lonlat]
            length = sum(
                math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
                for i in range(1, len(pts))
            )
            if length < min_path_len_m:
                continue
            if rotated:
                pts = [(x * cos_t - y * sin_t, x * sin_t + y * cos_t) for x, y in pts]
            polys.extend(_clip_polyline(pts, fx0, fy0, fx1, fy1))
        projected[layer] = polys

    # Canvas and mapping come from the frame, never the content.
    frame_w = max(fx1 - fx0, 1e-9)
    frame_h = max(fy1 - fy0, 1e-9)
    scale = width / frame_w
    height = frame_h * scale
    off_x, off_y = fx0, fy0
    stroke_w = max(0.5, width / 2000.0)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height:.1f}" viewBox="0 0 {width} {height:.2f}">',
    ]
    counts: dict[str, int] = {}
    for layer in layers:
        polys = projected.get(layer, [])
        counts[layer] = len(polys)
        parts.append(
            f'<g id="citymap-{layer}" fill="none" stroke="#000000" '
            f'stroke-width="{stroke_w:.2f}" stroke-linecap="round" stroke-linejoin="round">'
        )
        closed_ok = layer in AREA_LAYERS
        for pts in polys:
            mapped = [((x - off_x) * scale, (y - off_y) * scale) for x, y in pts]
            d = f"M {mapped[0][0]:.2f} {mapped[0][1]:.2f} " + " ".join(
                f"L {x:.2f} {y:.2f}" for x, y in mapped[1:]
            )
            if closed_ok and len(mapped) > 2:
                first, last = mapped[0], mapped[-1]
                if math.hypot(first[0] - last[0], first[1] - last[1]) < 1e-6:
                    d += " Z"
            parts.append(f"<path d=\"{d}\"/>")
        parts.append("</g>")
    parts.append("</svg>")
    return "\n".join(parts) + "\n", counts


__all__ = ["AREA_LAYERS", "project", "render_source_version", "render_svg"]
