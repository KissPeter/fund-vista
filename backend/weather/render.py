"""Compose the weather map artwork: one layered SVG, no frame, no title, no text.

Pure numpy and contourpy; no network, no cache. The sheet frame, title and
attribution lettering belong to ``POST /v1/compose-sheet`` (AGENTS.md "One
sheet source"). The output has one group per requested layer, each carrying
``data-layer`` so the shop can split inks later. Every drawn point is clipped to
the view. Coordinates are sheet millimetres (the viewBox is in mm), so path
lengths read straight off the output for the plot-time estimate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from backend.traffic.geo import clip_polyline
from backend.weather.config import (
    ARTWORK_INSET_MM,
    CITIES,
    CITY_DOT_MM,
    ISOTHERM_HEAVY_EVERY,
    LABEL_HEIGHT_MM,
    LABEL_PAD_MM,
    LAYER_IDS,
    MSLP_STEP_HPA,
    PLOT_SPEED_MM_S,
    SHEET_MM,
    STROKE_HEAVY_MM,
    STROKE_MM,
    T500_STEP_C,
    Z500_STEP_GPM,
    Region,
)
from backend.weather.contours import contour_lines, levels_between
from backend.weather.extrema import find_extrema
from backend.weather.geo import simplify
from backend.weather.geography import Geography
from backend.weather.lettering import Box, Polyline, choose_slot, lettering
from backend.weather.streamlines import streamlines

#: Marker letter cap height (mm), larger than the value labels.
MARKER_LETTER_MM = 3.2
#: Ramer-Douglas-Peucker tolerance on the sheet (mm) and the shortest piece kept.
SIMPLIFY_MM = 0.12
MIN_PIECE_MM = 0.3
#: Gap between a point and its label (mm).
LABEL_GAP_MM = 0.8

LAYER_LABELS = {
    "coast": "Coastline",
    "borders": "Borders",
    "streamlines": "Wind 250 hPa",
    "isobars": "MSLP isobars",
    "z500": "Z500 height",
    "isotherms": "T500 isotherms",
    "markers": "H and L",
    "cities": "Cities",
}


@dataclass(frozen=True)
class Frame:
    """Equirectangular view of the region box, fitted into the sheet (mm)."""

    lon0: float
    lon1: float
    lat0: float
    lat1: float
    kx: float
    scale: float
    off_x: float
    off_y: float
    span_x: float
    span_y: float

    @property
    def box(self) -> Box:
        return Box(self.off_x, self.off_y, self.off_x + self.span_x, self.off_y + self.span_y)

    def xy(self, lon, lat):
        x = self.off_x + (np.asarray(lon, dtype=float) - self.lon0) * self.kx * self.scale
        y = self.off_y + (self.lat1 - np.asarray(lat, dtype=float)) * self.scale
        return x, y

    def xy_point(self, lon: float, lat: float) -> tuple[float, float]:
        x, y = self.xy(lon, lat)
        return float(x), float(y)


def fit_frame(region: Region, orientation: str) -> Frame:
    """Fit the region box into the sheet inset. Crops longitude when the box is
    wider than the sheet (portrait), else centres it (letterboxed)."""
    sheet_w, sheet_h = SHEET_MM[orientation]
    rw = sheet_w - 2 * ARTWORK_INSET_MM
    rh = sheet_h - 2 * ARTWORK_INSET_MM
    lon0, lon1, lat0, lat1 = region.lon0, region.lon1, region.lat0, region.lat1
    kx = math.cos(math.radians((lat0 + lat1) / 2.0))
    aspect = rw / rh
    if (lon1 - lon0) * kx / (lat1 - lat0) > aspect:
        half = (lat1 - lat0) * aspect / kx / 2.0
        mid = (lon0 + lon1) / 2.0
        lon0, lon1 = mid - half, mid + half
    scale = min(rw / ((lon1 - lon0) * kx), rh / (lat1 - lat0))
    span_x = (lon1 - lon0) * kx * scale
    span_y = (lat1 - lat0) * scale
    return Frame(
        lon0=lon0, lon1=lon1, lat0=lat0, lat1=lat1, kx=kx, scale=scale,
        off_x=ARTWORK_INSET_MM + (rw - span_x) / 2.0,
        off_y=ARTWORK_INSET_MM + (rh - span_y) / 2.0,
        span_x=span_x, span_y=span_y,
    )


def _length(pl: Polyline) -> float:
    return float(sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pl, pl[1:])))


def _clip(pl: list[tuple[float, float]], box: Box) -> list[Polyline]:
    """Clip to ``box`` (reusing the traffic Liang-Barsky clip on local coordinates)."""
    shifted = [(x - box.x0, y - box.y0) for x, y in pl]
    pieces = clip_polyline(shifted, box.x1 - box.x0, box.y1 - box.y0)
    return [[(x + box.x0, y + box.y0) for x, y in piece] for piece in pieces]


def _emit_line(frame: Frame, lon, lat) -> list[Polyline]:
    x, y = frame.xy(lon, lat)
    pts = simplify(list(zip(x.tolist(), y.tolist())), SIMPLIFY_MM)
    return [p for p in _clip(pts, frame.box) if _length(p) >= MIN_PIECE_MM]


def _d(polylines: list[Polyline]) -> list[tuple[str, float]]:
    out = []
    for pl in polylines:
        d = "M" + " L".join("%.2f,%.2f" % (x, y) for x, y in pl)
        out.append((d, _length(pl)))
    return out


class _Ink:
    """Paths of one layer: (d, length_mm, stroke_width_or_None)."""

    def __init__(self) -> None:
        self.paths: list[tuple[str, float, float | None]] = []

    def add(self, polylines: list[Polyline], width: float | None = None) -> None:
        for d, mm in _d(polylines):
            self.paths.append((d, mm, width))

    def add_dot(self, x: float, y: float, r: float) -> None:
        d = "M%.2f,%.2f a%.2f,%.2f 0 1,0 %.2f,0 a%.2f,%.2f 0 1,0 %.2f,0" % (
            x - r, y, r, r, 2 * r, r, r, -2 * r)
        self.paths.append((d, 2 * math.pi * r, None))


@dataclass(frozen=True)
class Artwork:
    svg: str
    stats: dict
    warnings: list[str]


def compose_artwork(
    *,
    region: Region,
    orientation: str,
    layers: tuple[str, ...],
    cities: tuple[str, ...],
    geo: Geography,
    grid: tuple[np.ndarray, np.ndarray] | None = None,
    fields: dict[str, np.ndarray] | None = None,
) -> Artwork:
    """Compose the layered artwork. ``grid`` is ``(lat, lon)``; ``fields`` maps
    field names (see :data:`backend.weather.config.LAYER_FIELDS`) to ``(nlat, nlon)``.
    Only the requested layers are drawn, in :data:`LAYER_IDS` order."""
    fields = fields or {}
    wanted = set(layers)
    frame = fit_frame(region, orientation)
    warnings: list[str] = []
    inks: dict[str, _Ink] = {name: _Ink() for name in LAYER_IDS if name in wanted}

    lat_grid = lon_grid = None
    if grid is not None:
        lat_grid, lon_grid = np.asarray(grid[0], float), np.asarray(grid[1], float)

    if "coast" in wanted:
        for line in geo.coast:
            inks["coast"].add(_emit_line(frame, line[:, 0], line[:, 1]))
    if "borders" in wanted:
        for line in geo.borders:
            inks["borders"].add(_emit_line(frame, line[:, 0], line[:, 1]))

    if "streamlines" in wanted:
        u, v = fields["u250"], fields["v250"]
        for line in streamlines(u, v, lat_grid, lon_grid):
            inks["streamlines"].add(_emit_line(frame, line.lon, line.lat))

    if "isobars" in wanted:
        mslp = fields["mslp_hpa"]
        levels = levels_between(mslp, MSLP_STEP_HPA)
        for cl in contour_lines(mslp, lon_grid, lat_grid, levels):
            inks["isobars"].add(_emit_line(frame, cl.lon, cl.lat))

    if "z500" in wanted:
        z = fields["z500_gpm"]
        levels = levels_between(z, Z500_STEP_GPM)
        for cl in contour_lines(z, lon_grid, lat_grid, levels):
            inks["z500"].add(_emit_line(frame, cl.lon, cl.lat))

    if "isotherms" in wanted:
        t = fields["t500_c"]
        levels = levels_between(t, T500_STEP_C)
        for cl in contour_lines(t, lon_grid, lat_grid, levels, heavy_every=ISOTHERM_HEAVY_EVERY):
            width = STROKE_HEAVY_MM if cl.heavy else None
            inks["isotherms"].add(_emit_line(frame, cl.lon, cl.lat), width)

    # Labels: city dots and names first, then H/L markers (plan section 4: no
    # marker inside a city label box). Every label box must sit inside the view
    # and clear every box already placed.
    frame_box = frame.box
    taken: list[Box] = []
    if "cities" in wanted:
        ink = inks["cities"]
        dots: list[tuple[str, float, float]] = []
        for name in CITIES:  # config order: deterministic for any request order
            if name not in cities:
                continue
            lat, lon = CITIES[name]
            x, y = frame.xy_point(lon, lat)
            if not frame_box.x0 <= x <= frame_box.x1 or not frame_box.y0 <= y <= frame_box.y1:
                warnings.append("city_outside_view")
                continue
            dots.append((name, x, y))
            ink.add_dot(x, y, CITY_DOT_MM)
            taken.append(Box(x - CITY_DOT_MM, y - CITY_DOT_MM, x + CITY_DOT_MM, y + CITY_DOT_MM))
        for name, x, y in dots:
            text = lettering(name, LABEL_HEIGHT_MM)
            anchor = (x - CITY_DOT_MM, y - CITY_DOT_MM, x + CITY_DOT_MM, y + CITY_DOT_MM)
            slot = choose_slot(text, anchor, taken, frame_box, gap=LABEL_GAP_MM)
            if slot is None:
                warnings.append("city_label_skipped")
                continue
            taken.append(slot)
            ink.add(_clip_strokes(text.placed(slot.x0 + LABEL_PAD_MM, slot.y0 + LABEL_PAD_MM), frame_box))

    if "markers" in wanted:
        ink = inks["markers"]
        mslp = fields["mslp_hpa"]
        for ext in find_extrema(mslp, lon_grid, lat_grid):
            x, y = frame.xy_point(ext.lon, ext.lat)
            if not frame_box.x0 <= x <= frame_box.x1 or not frame_box.y0 <= y <= frame_box.y1:
                continue
            letter = lettering(ext.kind, MARKER_LETTER_MM)
            lw, lh = letter.width, letter.height
            letter_box = Box(x - lw / 2 - LABEL_PAD_MM, y - lh / 2 - LABEL_PAD_MM,
                             x + lw / 2 + LABEL_PAD_MM, y + lh / 2 + LABEL_PAD_MM)
            if any(letter_box.overlaps(t) for t in taken) or not letter_box.inside(frame_box):
                warnings.append("marker_dropped")
                continue
            taken.append(letter_box)
            ink.add(_clip_strokes(letter.placed(x - lw / 2, y - lh / 2), frame_box))
            value = lettering(str(int(round(ext.value_hpa))), LABEL_HEIGHT_MM)
            anchor = (x - lw / 2, y - lh / 2, x + lw / 2, y + lh / 2)
            slot = choose_slot(value, anchor, taken, frame_box, gap=LABEL_GAP_MM / 2)
            if slot is None:
                warnings.append("marker_value_skipped")
                continue
            taken.append(slot)
            ink.add(_clip_strokes(value.placed(slot.x0 + LABEL_PAD_MM, slot.y0 + LABEL_PAD_MM), frame_box))

    return _assemble(orientation, layers, inks, warnings)


def _clip_strokes(strokes: list[Polyline], box: Box) -> list[Polyline]:
    out: list[Polyline] = []
    for s in strokes:
        out.extend(_clip(s, box))
    return out


def _assemble(orientation: str, layers: tuple[str, ...], inks: dict[str, _Ink],
              warnings: list[str]) -> Artwork:
    sheet_w, sheet_h = SHEET_MM[orientation]
    head = ('<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
            'viewBox="0 0 %.0f %.0f" width="%.0fmm" height="%.0fmm">' % (sheet_w, sheet_h, sheet_w, sheet_h))
    out = [head]
    layer_stats: dict[str, dict] = {}
    total_paths = 0
    total_mm = 0.0
    for name in LAYER_IDS:
        if name not in inks:
            continue
        ink = inks[name]
        mm = sum(length for _d_, length, _w in ink.paths)
        layer_stats[name] = {
            "paths": len(ink.paths),
            "mm": round(mm, 1),
            "minutes": round(mm / PLOT_SPEED_MM_S / 60.0, 2),
        }
        total_paths += len(ink.paths)
        total_mm += mm
        out.append('<g id="%s" data-layer="%s" inkscape:groupmode="layer" inkscape:label="%s" '
                   'fill="none" stroke="#000000" stroke-width="%.2f" stroke-linejoin="round" '
                   'stroke-linecap="round">' % (name, name, LAYER_LABELS[name], STROKE_MM))
        for d, _length, width in ink.paths:
            if width is None:
                out.append('<path d="%s"/>' % d)
            else:
                out.append('<path d="%s" stroke-width="%.2f"/>' % (d, width))
        out.append("</g>")
    out.append("</svg>")
    stats = {
        "layers": layer_stats,
        "total": {
            "paths": total_paths,
            "mm": round(total_mm, 1),
            "minutes": round(total_mm / PLOT_SPEED_MM_S / 60.0, 2),
        },
        "plot_speed_mm_s": PLOT_SPEED_MM_S,
        "orientation": orientation,
    }
    return Artwork(svg="\n".join(out), stats=stats, warnings=sorted(set(warnings)))


__all__ = ["Artwork", "Frame", "compose_artwork", "fit_frame"]
