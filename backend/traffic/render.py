"""Chrome-free, pen-plotter SVG of filtered airport movements over the OSM basemap.

Pure port of the ``tools/airport_traffic/render.py`` POC — geometry and
layer logic unchanged (plan Phase 2). One Inkscape layer
(``<g inkscape:groupmode="layer">``) per pen: motorways, waterways, water,
airfield, departures, arrivals, holding, go-around — plus Phase 5
wide/narrow/regional/other layers used instead of arrivals/departures when
``pen_mode="category"``. The view is a lat/lon
bbox ``(south, north, west, east)``, so the same function renders the whole
country or a zoomed airport.

Issue #1080 added two things on top of the port:

* the **Map** layer (the basemap groups: motorways / waterways / water) and
  the **Airport** layer (the airfield group + the airport marker circle) are
  switchable — an unticked layer drops its group from the SVG;
* the per-layer **counts are potential counts**: they are counted before the
  layer enable/disable filter, so a layer reports how many lines it would
  contribute even while it is unticked (the drawn SVG still respects the
  toggles).
"""

from __future__ import annotations

import math
from dataclasses import replace
from xml.sax.saxutils import escape

from backend.traffic.filters import Filters, pen
from backend.traffic.geo import clip_polyline, rdp

#: Bump when renderer semantics change — part of the L4 SVG cache key so a
#: fix recomputes instead of serving stale art (plan §6.3).
#: r2 (Phase 5): category pen mode (wide/narrow/regional/other layers).
#: r3 (issue #1080): the Map/Airport layer switches + potential per-layer
#: counts (counted before the layer filter).
RENDER_VERSION = "r3"

PENS = {  # layer id -> (label, stroke, width)   widths are in SVG user units of a 1000-wide canvas
    "motorways": ("Motorways", "#8a6d3b", 0.9),
    "waterways": ("Waterways", "#5aa0c8", 0.7),
    "water": ("Lakes", "#5aa0c8", 0.9),
    "airfield": ("Airfield", "#000000", 1.0),
    "departures": ("Departures", "#2c6fbb", 0.7),
    "arrivals": ("Arrivals", "#222222", 0.7),
    "wide": ("Wide-body", "#6a3ec0", 0.7),
    "narrow": ("Narrow-body", "#2c6fbb", 0.7),
    "regional": ("Regional", "#1a936f", 0.7),
    "other": ("Other", "#8a8a8a", 0.7),
    "holding": ("Holding", "#e07a00", 1.4),
    "go_around": ("Go-around", "#d6204a", 1.4),
}
#: The basemap groups behind the "Map" layer (issue #1080).
MAP_LAYERS = ("motorways", "waterways", "water")
#: The airfield group behind the "Airport" layer (issue #1080) — the airport
#: marker circle belongs to it.
AIRPORT_LAYERS = ("airfield",)
BASEMAP_LAYERS = MAP_LAYERS + AIRPORT_LAYERS
KIND_LAYERS = ("departures", "arrivals")
CATEGORY_LAYERS = ("wide", "narrow", "regional", "other")
HIGHLIGHT_LAYERS = ("holding", "go_around")
ORDER = BASEMAP_LAYERS + KIND_LAYERS + CATEGORY_LAYERS + HIGHLIGHT_LAYERS


def view_bbox(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """South, north, west, east around the airport (zoom in = smaller radius)."""
    dlat = radius_km / 111.2
    dlon = dlat / math.cos(math.radians(lat))
    return (lat - dlat, lat + dlat, lon - dlon, lon + dlon)


class Projection:
    def __init__(self, bbox: tuple[float, float, float, float], width: float = 1000.0) -> None:
        self.s, self.n, self.w, self.e = bbox
        self.kx = math.cos(math.radians((self.s + self.n) / 2))
        self.scale = width / ((self.e - self.w) * self.kx)
        self.width, self.height = width, (self.n - self.s) * self.scale

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        return ((lon - self.w) * self.kx * self.scale, (self.n - lat) * self.scale)

    def path(self, lonlat: list, eps: float) -> list[str]:
        pts = rdp([self.xy(la, lo) for lo, la in lonlat], eps)
        return ["M" + " L".join("%.1f,%.1f" % q for q in seg) for seg in clip_polyline(pts, self.width, self.height)]


def render_svg(movements: list[dict], basemap: dict, bbox: tuple[float, float, float, float],
               flt: Filters, airport: dict | None = None, width: float = 1000.0,
               include_map: bool = True, include_airport: bool = True) -> tuple[str, dict]:
    """Render the traffic SVG + the per-layer **potential** counts.

    ``include_map`` / ``include_airport`` are the Map / Airport layer
    switches (issue #1080): an unticked layer drops its group from the SVG
    (the group ids never change).

    The counts are *potential*: every movement is counted in the layer it
    would land in when the layer toggles are all on, so a layer reports how
    many lines it would contribute even while it is unticked. The drawn
    paths still respect the toggles.
    """
    proj = Projection(bbox, width)
    eps_base, eps_trk = 0.3, 0.35
    layers: dict[str, list[str]] = {k: [] for k in ORDER}
    for k in BASEMAP_LAYERS:
        if k in MAP_LAYERS and not include_map:
            continue
        if k in AIRPORT_LAYERS and not include_airport:
            continue
        for line in basemap.get(k, []):
            layers[k] += ['<path d="%s"/>' % d for d in proj.path(line, eps_base)]
    move_layers = (CATEGORY_LAYERS if flt.pen_mode == "category" else KIND_LAYERS) + HIGHLIGHT_LAYERS
    counts = {k: 0 for k in move_layers}
    # The potential-count pass: the layer toggles are neutralised, every
    # other filter (window, military, privacy, type, airline, …) still
    # applies. The counts therefore do not depend on the toggles — the
    # chip keeps its number when the layer is switched off.
    potential = replace(flt, arrivals=True, departures=True, holding=True, go_around=True)
    # With every layer already on, the potential layer IS the drawn one —
    # the second pass would only repeat it.
    all_layers_on = potential == flt
    ap = airport or {}
    ag = (float(ap.get("lat", 0.0)), float(ap.get("lon", 0.0)), float(ap.get("elev_ft", 0.0)))
    for f in movements:
        layer = pen(f, flt, ag)
        possible = layer if all_layers_on else pen(f, potential, ag)
        if possible is not None:
            counts[possible] += 1
        if layer is None:
            continue
        xy = [proj.xy(p[1], p[2]) for p in f["pts"]]
        title = escape(" ".join(x for x in (f.get("flight") or f["hex"], f.get("type") or "") if x))
        for seg in clip_polyline(rdp(xy, eps_trk), proj.width, proj.height):
            layers[layer].append('<path d="M%s"><title>%s</title></path>' % (" L".join("%.1f,%.1f" % q for q in seg), title))
    if airport and include_airport:  # marker so the airport stays findable at country zoom
        x, y = proj.xy(airport["lat"], airport["lon"])
        if 0 <= x <= proj.width and 0 <= y <= proj.height:
            layers["airfield"].append('<circle cx="%.1f" cy="%.1f" r="4"/>' % (x, y))
    out = ['<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
           'viewBox="0 0 %.0f %.0f" width="%.0fmm" height="%.0fmm">' % (proj.width, proj.height, proj.width * 0.3, proj.height * 0.3)]
    for k in ORDER:
        if k in MAP_LAYERS and not include_map:
            continue
        if k in AIRPORT_LAYERS and not include_airport:
            continue
        label, stroke, sw = PENS[k]
        out.append('<g id="traffic-%s" inkscape:groupmode="layer" inkscape:label="%s" fill="none" stroke="%s" '
                   'stroke-width="%.2f" stroke-linejoin="round" stroke-linecap="round">' % (k, label, stroke, sw))
        out += layers[k]
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out), counts
