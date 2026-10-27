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

* the **Map** layer (the basemap groups: country / motorways / waterways /
  water) and the **Airport** layer (the airfield group + the airport marker
  circle) are switchable — an unticked layer drops its group from the SVG;
* the per-layer **counts are potential counts**: they are counted before the
  layer enable/disable filter, so a layer reports how many lines it would
  contribute even while it is unticked (the drawn SVG still respects the
  toggles).

r4 (paper-fill) adds two framing knobs on top, mirroring the citymap
renderer's vocabulary:

* ``viewport_aspect`` — the SVG canvas gets exactly this width/height
  ratio (the paper's shape, e.g. A4 landscape 1.414); the bbox content is
  fit-contain centred into it, so ``POST /v1/convert`` fills the sheet
  instead of leaving one axis empty. The router grows the view bbox to the
  aspect first (``expand_view_to_aspect``) so the extra canvas area carries
  map wherever the loaded day's bbox allows.
* ``min_path_len_m`` — projected polylines shorter than this (in meters,
  resolved through the projection scale, so the knob is resolution
  independent) are dropped from every layer, basemap and movements alike:
  the city-roads ``minLength`` pen-plotter fast path.
"""

from __future__ import annotations

import math
from dataclasses import replace
from xml.sax.saxutils import escape

from backend.traffic.corridors import corridor_keep
from backend.traffic.filters import Filters, pen
from backend.traffic.geo import clip_polyline, rdp

#: Bump when renderer semantics change — part of the L4 SVG cache key so a
#: fix recomputes instead of serving stale art (plan §6.3).
#: r2 (Phase 5): category pen mode (wide/narrow/regional/other layers).
#: r3 (issue #1080): the Map/Airport layer switches + potential per-layer
#: counts (counted before the layer filter).
#: r2 (Phase 5): category pen mode (wide/narrow/regional/other layers).
#: r3 (issue #1080): the Map/Airport layer switches + potential per-layer
#: counts (counted before the layer filter).
#: r4 (corridor collapse): near-identical movement strokes merge into
#: corridor representatives per layer — the runway funnel no longer re-inks
#: one line dozens of times.
#: r5 (paper fill): the country border basemap layer (rides the Map
#: switch), the viewport-aspect framing and the min-path-length detail
#: filter. Bumped so cached r4 SVGs (no border, bbox-ratio canvas,
#: unfiltered stubs) expire.
RENDER_VERSION = "r5"

PENS = {  # layer id -> (label, stroke, width)   widths are in SVG user units of a 1000-wide canvas
    "country": ("Country border", "#000000", 0.8),
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
#: The basemap groups behind the "Map" layer (issue #1080). The country
#: border is first — drawn (and plotted) under everything else.
MAP_LAYERS = ("country", "motorways", "waterways", "water")
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
    """Equirectangular lat/lon → SVG-user-unit mapping for one canvas.

    ``aspect`` (width/height) frames the canvas to the paper's shape: the
    bbox content is scaled to *fit contain* and centred, so the drawn map
    never distorts and the canvas always has the requested ratio (the
    residual mismatch shows as an even margin on one axis — the router
    grows the view bbox first so the margin stays small). Without an
    aspect the canvas keeps the bbox ratio (the historical behaviour).
    """

    def __init__(self, bbox: tuple[float, float, float, float],
                 width: float = 1000.0, aspect: float | None = None) -> None:
        self.s, self.n, self.w, self.e = bbox
        self.kx = math.cos(math.radians((self.s + self.n) / 2))
        span_x = max((self.e - self.w) * self.kx, 1e-12)
        span_y = max(self.n - self.s, 1e-12)
        if aspect:
            self.height = width / aspect
            self.scale = min(width / span_x, self.height / span_y)
            self.off_x = (width - span_x * self.scale) / 2.0
            self.off_y = (self.height - span_y * self.scale) / 2.0
        else:
            self.scale = width / span_x
            self.height = span_y * self.scale
            self.off_x = self.off_y = 0.0
        self.width = width

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        return ((lon - self.w) * self.kx * self.scale + self.off_x,
                (self.n - lat) * self.scale + self.off_y)

    def path(self, lonlat: list, eps: float, min_units: float = 0.0) -> list[str]:
        pts = rdp([self.xy(la, lo) for lo, la in lonlat], eps)
        out: list[str] = []
        for seg in clip_polyline(pts, self.width, self.height):
            if path_units(seg) < min_units:
                continue  # sub-detail stub: invisible ink, real plot time
            out.append("M" + " L".join("%.1f,%.1f" % q for q in seg))
        return out


def path_units(seg: list[tuple[float, float]]) -> float:
    """Polyline length in SVG user units (the min-path-length yardstick)."""
    return sum(math.hypot(seg[i][0] - seg[i - 1][0], seg[i][1] - seg[i - 1][1])
               for i in range(1, len(seg)))


def expand_view_to_aspect(
    view: tuple[float, float, float, float],
    aspect: float,
    limits: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Grow a view around its centre to ``aspect`` (width/height), clamped.

    The ingest clips the day's traces to its own bbox (``limits``), so
    nothing beyond can be drawn — the growth stops there and whatever
    residual mismatch remains letterboxes in the SVG (Projection's
    fit-contain centre). One axis grows per call: whichever the ratio
    demands. Never shrinks a user-picked extent.
    """
    s, n, w, e = view
    ls, ln, lw, le = limits
    kx = math.cos(math.radians((s + n) / 2))
    span_x = (e - w) * kx
    span_y = n - s
    if span_x / max(span_y, 1e-12) < aspect:  # too tall → widen east–west
        need = (span_y * aspect - span_x) / kx / 2.0
        w2 = max(lw, w - need)
        e2 = min(le, e + need)
        slack = (w - need) - w2  # the west hit the limit → push east
        if slack > 1e-12:
            e2 = min(le, e2 + slack)
        slack = (e + need) - e2
        if slack > 1e-12:
            w2 = max(lw, w2 - slack)
        return (s, n, w2, e2)
    need = (span_x / aspect - span_y) / 2.0  # too wide → grow north–south
    s2 = max(ls, s - need)
    n2 = min(ln, n + need)
    slack = (s - need) - s2
    if slack > 1e-12:
        n2 = min(ln, n2 + slack)
    slack = (n + need) - n2
    if slack > 1e-12:
        s2 = max(ls, s2 - slack)
    return (s2, n2, w, e)


def render_svg(movements: list[dict], basemap: dict, bbox: tuple[float, float, float, float],
               flt: Filters, airport: dict | None = None, width: float = 1000.0,
               include_map: bool = True, include_airport: bool = True,
               viewport_aspect: float | None = None, min_path_len_m: float = 0.0,
               ) -> tuple[str, dict]:
    """Render the traffic SVG + the per-layer **potential** counts.

    ``include_map`` / ``include_airport`` are the Map / Airport layer
    switches (issue #1080): an unticked layer drops its group from the SVG
    (the group ids never change).

    ``viewport_aspect`` (r5) frames the canvas to the paper's shape —
    width/height — with the bbox content fit-contain centred (see
    :class:`Projection`); ``min_path_len_m`` (r5) drops projected pieces
    shorter than that many meters, on every layer (the pen-plotter detail
    filter; meters resolve through the projection scale, so the knob means
    the same thing at every ``width`` and zoom — and before the corridor
    merge, so it never pays Hausdorff for debris).

    The counts are *potential*: every movement is counted in the layer it
    would land in when the layer toggles are all on, so a layer reports how
    many lines it would contribute even while it is unticked. The drawn
    paths still respect the toggles.

    The **corridor collapse** (issue: runway-funnel line storm) merges
    near-identical strokes of a layer into corridor representatives before
    they are emitted, so a busy approach corridor does not re-ink the same
    line dozens of times. The counts stay *movement* counts — zero always
    means "no movements", never "all merged away".
    """
    proj = Projection(bbox, width, viewport_aspect)
    eps_base, eps_trk = 0.3, 0.35
    # Meters per SVG user unit: the projection scales ``width`` units over
    # the lon span, so 1 unit covers (e-w)*111.2 km*kx / width km of ground
    # — resolution independent, same meters at every zoom and canvas width.
    min_units = min_path_len_m * proj.scale / 111200.0 if min_path_len_m > 0 else 0.0
    layers: dict[str, list[str]] = {k: [] for k in ORDER}
    for k in BASEMAP_LAYERS:
        if k in MAP_LAYERS and not include_map:
            continue
        if k in AIRPORT_LAYERS and not include_airport:
            continue
        for line in basemap.get(k, []):
            layers[k] += ['<path d="%s"/>' % d for d in proj.path(line, eps_base, min_units)]
    move_layers = (CATEGORY_LAYERS if flt.pen_mode == "category" else KIND_LAYERS) + HIGHLIGHT_LAYERS
    counts = {k: 0 for k in move_layers}
    segs_by_layer: dict[str, list[tuple[list[tuple[float, float]], str]]] = {k: [] for k in move_layers}
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
            if path_units(seg) < min_units:
                continue  # sub-detail stub: invisible ink, real plot time
            segs_by_layer[layer].append((seg, title))
    if airport and include_airport:  # marker so the airport stays findable at country zoom
        x, y = proj.xy(airport["lat"], airport["lon"])
        if 0 <= x <= proj.width and 0 <= y <= proj.height:
            layers["airfield"].append('<circle cx="%.1f" cy="%.1f" r="4"/>' % (x, y))
    # Corridor collapse per movement layer (runway funnel): survivors keep
    # their clipped segments and their title verbatim.
    for k, arcs in segs_by_layer.items():
        for i in corridor_keep([arc for arc, _t in arcs], proj.width):
            seg, title = arcs[i]
            layers[k].append('<path d="M%s"><title>%s</title></path>' % (" L".join("%.1f,%.1f" % q for q in seg), title))
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
