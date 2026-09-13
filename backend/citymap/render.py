"""Plane projection + plotter-friendly SVG rendering.

Lon/lat polylines are projected with an equirectangular approximation
around the bbox center (the same flat-plane idea as city-roads' Grid —
good to ~1% over a metro area) and fitted into ``width`` user units. Each
requested layer becomes one ``<g id="citymap-<layer>">`` of stroked paths;
area layers (buildings, water, aeroway) emit closed outlines, everything
else open polylines. Fills are always ``none`` so the output plots cleanly.

The output is chrome-free by construction: no location caption, no
attribution comment. (For SVGs exported by the upstream city-roads app,
which do carry those, see :mod:`backend.citymap.chrome`.) The ODbL credit
lives in the API response metadata instead (``attribution`` field).
"""

from __future__ import annotations

import math

from backend.citymap.overpass import BBox

# Layers whose closed rings are outlines of areas (emit a closing Z).
AREA_LAYERS = frozenset({"buildings", "water", "aeroway"})

_M_PER_DEG_LAT = 110540.0
_M_PER_DEG_LON_EQUATOR = 111320.0


def project(
    lon: float, lat: float, lon0: float, lat0: float
) -> tuple[float, float]:
    """Equirectangular projection to plane meters around (lon0, lat0)."""
    x = (lon - lon0) * _M_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat0))
    y = (lat0 - lat) * _M_PER_DEG_LAT  # SVG y grows downwards = south
    return x, y


def render_svg(
    geoms: dict[str, list[list[tuple[float, float]]]],
    bbox: BBox,
    layers: list[str],
    *,
    width: int = 1000,
    min_path_len_m: float = 0.0,
    rotation_deg: float = 0.0,
) -> tuple[str, dict[str, int]]:
    """Render layer geometries to a chrome-free SVG document.

    Returns ``(svg_text, path_counts)``. Polylines shorter than
    ``min_path_len_m`` meters are dropped (the city-roads ``minLength``
    pen-plotter option, but in meters instead of pixels). The artwork is
    rotated ``rotation_deg`` degrees clockwise around the bbox center
    before fitting — bounds are recomputed after rotation so nothing is
    clipped.
    """
    south, west, north, east = bbox
    lon0, lat0 = (west + east) / 2.0, (south + north) / 2.0
    theta = math.radians(rotation_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    projected: dict[str, list[list[tuple[float, float]]]] = {}
    min_x = min_y = math.inf
    max_x = max_y = -math.inf
    for layer in layers:
        polys: list[list[tuple[float, float]]] = []
        for lonlat in geoms.get(layer, []):
            pts = [project(lon, lat, lon0, lat0) for lon, lat in lonlat]
            length = sum(
                math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
                for i in range(1, len(pts))
            )
            if length < min_path_len_m:
                continue
            if theta:
                pts = [
                    (x * cos_t - y * sin_t, x * sin_t + y * cos_t) for x, y in pts
                ]
            polys.append(pts)
            for x, y in pts:
                min_x, max_x = min(min_x, x), max(max_x, x)
                min_y, max_y = min(min_y, y), max(max_y, y)
        projected[layer] = polys

    span_x = max(max_x - min_x, 1e-9)
    span_y = max(max_y - min_y, 1e-9)
    scale = width / span_x
    height = span_y * scale
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
            mapped = [((x - min_x) * scale, (y - min_y) * scale) for x, y in pts]
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


__all__ = ["AREA_LAYERS", "project", "render_svg"]
