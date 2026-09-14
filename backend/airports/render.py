"""Blueprint-style SVG rendering for airport ground diagrams.

Layout (client-approved reference): title header, column frequency strip
with rule, diagram area, footer — all strokes, ``fill="none"`` (except the
arrowhead marker, structurally needed). Stays black-on-paper: the plotter
draws lines, not fills — the white-on-navy look of the reference mock is a
display style, available via the page's Display background picker.

Runway boldness is GEOMETRY, not ``stroke-width``: each strip is drawn as
a solid band of nine parallel longitudinal paths across twice the true
width (2× ``width_ft`` schematic exaggeration — at poster scale the true
45 m strip is ~1.6 mm wide and unreadable). At typical pen/plot scales the
~0.3 mm line pitch merges into a near-solid black bar, making the runway
the heaviest feature through ``parse_svg_vectors`` (which discards stroke
widths), vpype and the single-ink plot. The heavy ``stroke-width`` on the
group only affects the raw-SVG preview.

Text stays as ``<text>`` (monospace): Inkscape converts it to paths before
plotting via the iDraw extension — same step that already handles title
blocks for the other product lines.
"""

from __future__ import annotations

import hashlib
import inspect
import math
import sys
from functools import lru_cache
from xml.sax.saxutils import escape as _xml_escape

from backend.airports.geometry import (
    project,
    rotate_point,
    rotation_for_heading,
    runway_heading_deg,
)
from backend.airports.ourairports import FT_TO_M, heading_from_ident
from backend.airports.overpass import CONTEXT_CLASSES
from backend.airports.schemas import AIRFIELD_LAYERS

# Version of the rendered output, baked into the SVG cache key so clients
# never see a stale layout. Derived from this file's own source: EVERY code
# change busts the cache automatically, no manual bump to forget.
@lru_cache(maxsize=1)
def render_source_version() -> str:
    return hashlib.sha1(
        inspect.getsource(sys.modules[__name__]).encode("utf-8")
    ).hexdigest()[:12]

# A4 portrait in user units at 1000 wide → height set by content; the plotter
# scales the viewBox to the page with margin (iDraw working area 210×297mm).
# NOTE: no title, no frequency strip, no footer in the artwork — the SVG is
# pure diagram geometry (runways, ground, badges, compass). Names,
# frequencies and credits live on the page / API responses, never plotted.
_MARGIN = 40.0

# Runway strip treatment: schematic 2× width exaggeration + 9 longitudinal
# infill lines. True-width strips vanish at poster scale; the exaggerated
# band with ~0.3 mm line pitch plots as a near-solid black bar.
_RUNWAY_WIDTH_EXAGGERATE = 2.0
_RUNWAY_INFILL_FRACS = tuple(-1.0 + 2.0 * i / 8 for i in range(9))


def _esc(text: object) -> str:
    return _xml_escape(str(text), {'"': "&quot;"})


def _fnum(value: object) -> float | None:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def render_diagram(
    *,
    airport: dict,
    runways: list[dict],
    frequencies: list[dict],
    osm_geoms: dict[str, list[list[tuple[float, float]]]],
    context_geoms: dict[str, list[list[tuple[float, float]]]] | None = None,
    width: int = 1000,
    min_path_len_m: float = 5.0,
    zoom: float = 1.0,
    layers: list[str] | None = None,
    taxiway_refs: list[tuple[str, list[tuple[float, float]]]] | None = None,
    taxiway_labels: bool = False,
) -> tuple[str, dict[str, int], float, list[str]]:
    """Render the pure-diagram SVG (no title/strip/footer — those live on
    the page and in API responses, never plotted).

    Returns ``(svg_text, path_counts, rotation_deg, warnings)``. ``airport``
    needs ``latitude_deg/longitude_deg``. ``runways`` are raw ``runways.csv``
    rows; endpoints come from :func:`runway_endpoints` (authoritative coords
    or ident-heading fallback). ``frequencies`` is accepted for signature
    stability but not drawn. ``taxiway_refs`` are ``(ref, way-lonlat)`` pairs
    (see :func:`extract_taxiway_refs`); a ref draws iff its way survives the
    min-detail filter and the taxiway layer is selected.
    """
    from backend.airports.ourairports import runway_endpoints

    warnings: list[str] = []
    selected = set(layers) if layers is not None else set(AIRFIELD_LAYERS)
    lat0 = float(airport["latitude_deg"])
    lon0 = float(airport["longitude_deg"])

    def proj(lonlat: tuple[float, float]) -> tuple[float, float]:
        return project(lonlat[0], lonlat[1], lon0, lat0)

    # -- authoritative runway strips -------------------------------------
    strips: list[dict] = []
    for row in runways:
        try:
            le_ll, he_ll, derived = runway_endpoints(row, lat0, lon0)
        except Exception:
            continue
        le, he = proj(le_ll), proj(he_ll)
        length_m = (_fnum(row.get("length_ft")) or 0.0) * FT_TO_M
        width_m = (_fnum(row.get("width_ft")) or 0.0) * FT_TO_M
        if width_m <= 0:
            width_m = 45.0  # standard runway width fallback
            warnings.append("runway_width_fallback")
        if derived:
            warnings.append("runway_endpoints_derived")
        le_ident = (row.get("le_ident") or "").strip()
        he_ident = (row.get("he_ident") or "").strip()
        strips.append(
            {
                "le": le,
                "he": he,
                "width_m": width_m,
                "length_m": length_m,
                "le_ident": le_ident,
                "he_ident": he_ident,
                "le_deg": _fnum(row.get("le_heading_degT")) or heading_from_ident(le_ident),
                "he_deg": _fnum(row.get("he_heading_degT")) or heading_from_ident(he_ident),
                "le_disp_m": (_fnum(row.get("le_displaced_threshold_ft")) or 0.0) * FT_TO_M,
                "he_disp_m": (_fnum(row.get("he_displaced_threshold_ft")) or 0.0) * FT_TO_M,
            }
        )

    # -- single scene rotation from the primary (longest) runway ----------
    if strips:
        primary = max(strips, key=lambda s: s["length_m"])
        heading = runway_heading_deg(primary["le"], primary["he"])
        rotation = rotation_for_heading(heading)
    else:
        rotation = 0.0
        warnings.append("no_runway_data")

    strips_r = [
        {**s, "le": rotate_point(*s["le"], rotation), "he": rotate_point(*s["he"], rotation)}
        for s in strips
    ]

    def _project_polys(
        polys: list[list[tuple[float, float]]],
        closed: bool = False,
    ) -> list[list[tuple[float, float]]]:
        kept: list[list[tuple[float, float]]] = []
        for lonlat in polys:
            pts = [proj(ll) for ll in lonlat]
            if closed:
                # Area outlines: perimeter misleads (a 12×12 m shed has a
                # ~48 m perimeter and would survive a 30 m cutoff). Filter
                # by footprint instead — longest bbox side in meters, so
                # "min detail 30 m" hides anything smaller than 30 m across.
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                if max(max(xs) - min(xs), max(ys) - min(ys)) < min_path_len_m:
                    continue
            else:
                length = sum(
                    math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
                    for i in range(1, len(pts))
                )
                if length < min_path_len_m:
                    continue
            kept.append([rotate_point(x, y, rotation) for x, y in pts])
        return kept

    # Closed area outlines (filter by footprint extent); everything else is
    # an open centerline/polyline (filter by path length).
    _AREA_CLASSES = frozenset({
        "runway", "apron", "terminal", "hangar", "stands", "stopways",
        "water", "buildings",
    })
    osm_r: dict[str, list[list[tuple[float, float]]]] = {}
    for cls, polys in osm_geoms.items():
        osm_r[cls] = _project_polys(polys, closed=cls in _AREA_CLASSES)
    if not any(osm_r.values()):
        warnings.append("no_osm_aeroway")
    ctx_r: dict[str, list[list[tuple[float, float]]]] = {}
    if context_geoms:
        for cls, polys in context_geoms.items():
            ctx_r[cls] = _project_polys(polys, closed=cls in _AREA_CLASSES)
        if not any(ctx_r.values()):
            warnings.append("no_context_data")

    # -- fit rotated world → diagram area (SELECTED layers only) ---------
    all_pts: list[tuple[float, float]] = []
    if "runway" in selected:
        for s in strips_r:
            all_pts += [s["le"], s["he"]]
    for cls, polys in list(osm_r.items()):
        if cls in selected:
            for poly in polys:
                all_pts += poly
    for cls, polys in list(ctx_r.items()):
        if cls in selected:
            for poly in polys:
                all_pts += poly
    if not all_pts:
        all_pts = [(-500.0, -500.0), (500.0, 500.0)]
    min_x = min(p[0] for p in all_pts)
    max_x = max(p[0] for p in all_pts)
    min_y = min(p[1] for p in all_pts)
    max_y = max(p[1] for p in all_pts)
    pad_m = max(max_x - min_x, max_y - min_y) * 0.06 + 50.0
    min_x -= pad_m
    max_x += pad_m
    min_y -= pad_m
    max_y += pad_m

    header_h = 0.0
    diagram_w = width - 2 * _MARGIN
    # Fixed frame: the page rect is sized by the zoom=1.0 fit and never
    # grows with zoom. Zoom only scales content about the center, so
    # zoom>1 crops ALL sides evenly (it used to grow diagram_h with the
    # scale, cropping left/right only and letterboxing tall SVGs into a
    # thin middle band downstream).
    base_scale = diagram_w / max(max_x - min_x, 1e-9)
    scale = base_scale * zoom
    # Centered mapping (identical to corner fit at zoom=1.0): zooming
    # crops/expands about the content center so the page fills evenly.
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    diagram_h = (max_y - min_y) * base_scale
    total_h = _MARGIN + header_h + _MARGIN / 2 + diagram_h + _MARGIN
    origin_y = _MARGIN + header_h + _MARGIN / 2

    def W2S(x: float, y: float) -> tuple[float, float]:
        # World (+x east, +y north) → SVG (+x right, +y down).
        return (_MARGIN + diagram_w / 2 + (x - cx) * scale,
                origin_y + diagram_h / 2 - (y - cy) * scale)

    # -- frame clipping (SVG space): zoomed geometry is cut at the diagram
    # border, never drawn into the margins. Open polylines use
    # Cohen–Sutherland (a line crossing the frame becomes visible runs);
    # closed outlines use Sutherland–Hodgman. Done geometrically — not via
    # clip-path, which the vector/plotter pipeline would ignore.
    _CX0, _CY0 = _MARGIN, origin_y
    _CX1, _CY1 = _MARGIN + diagram_w, origin_y + diagram_h

    def _outcode(x: float, y: float) -> int:
        code = 0
        if x < _CX0:
            code |= 1
        elif x > _CX1:
            code |= 2
        if y < _CY0:
            code |= 4
        elif y > _CY1:
            code |= 8
        return code

    def _clip_segment(
        p: tuple[float, float], q: tuple[float, float]
    ) -> tuple[tuple[float, float], tuple[float, float]] | None:
        x0, y0 = p
        x1, y1 = q
        c0, c1 = _outcode(x0, y0), _outcode(x1, y1)
        while True:
            if not (c0 | c1):
                return (x0, y0), (x1, y1)
            if c0 & c1:
                return None
            c = c0 if c0 else c1
            if c & 8:
                x = x0 + (x1 - x0) * (_CY1 - y0) / (y1 - y0) if y1 != y0 else x0
                y = _CY1
            elif c & 4:
                x = x0 + (x1 - x0) * (_CY0 - y0) / (y1 - y0) if y1 != y0 else x0
                y = _CY0
            elif c & 2:
                y = y0 + (y1 - y0) * (_CX1 - x0) / (x1 - x0) if x1 != x0 else y0
                x = _CX1
            else:
                y = y0 + (y1 - y0) * (_CX0 - x0) / (x1 - x0) if x1 != x0 else y0
                x = _CX0
            if c == c0:
                x0, y0, c0 = x, y, _outcode(x, y)
            else:
                x1, y1, c1 = x, y, _outcode(x, y)

    def _clip_open(pts: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
        runs: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        for i in range(len(pts) - 1):
            seg = _clip_segment(pts[i], pts[i + 1])
            if seg is None:
                if len(current) >= 2:
                    runs.append(current)
                current = []
                continue
            a, b = seg
            if current and current[-1] == a:
                current.append(b)
            else:
                if len(current) >= 2:
                    runs.append(current)
                current = [a, b]
        if len(current) >= 2:
            runs.append(current)
        return runs

    def _clip_edge(
        pts: list[tuple[float, float]], inside, intersect
    ) -> list[tuple[float, float]]:
        if not pts:
            return []
        out = [pts[0]] if inside(pts[0]) else []
        for i in range(1, len(pts)):
            prev, cur = pts[i - 1], pts[i]
            cur_in, prev_in = inside(cur), inside(prev)
            if cur_in:
                if not prev_in:
                    out.append(intersect(prev, cur))
                out.append(cur)
            elif prev_in:
                out.append(intersect(prev, cur))
        return out

    def _clip_closed(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
        ring = list(pts)
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        for inside, intersect in (
            (lambda p: p[0] >= _CX0,
             lambda a, b: (_CX0, a[1] + (b[1] - a[1]) * (_CX0 - a[0]) / (b[0] - a[0]))),
            (lambda p: p[0] <= _CX1,
             lambda a, b: (_CX1, a[1] + (b[1] - a[1]) * (_CX1 - a[0]) / (b[0] - a[0]))),
            (lambda p: p[1] >= _CY0,
             lambda a, b: (a[0] + (b[0] - a[0]) * (_CY0 - a[1]) / (b[1] - a[1]), _CY0)),
            (lambda p: p[1] <= _CY1,
             lambda a, b: (a[0] + (b[0] - a[0]) * (_CY1 - a[1]) / (b[1] - a[1]), _CY1)),
        ):
            ring = _clip_edge(ring, inside, intersect)
            if len(ring) < 3:
                return []
        return ring

    def _in_frame(x: float, y: float) -> bool:
        return _CX0 <= x <= _CX1 and _CY0 <= y <= _CY1

    # -- stroke weights (preview only; hierarchy survives via geometry) ----
    runway_w = max(6.0, width / 130.0)
    taxi_w = max(1.2, width / 700.0)
    thin_w = max(0.8, width / 1100.0)
    text_h = max(11.0, width / 72.0)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{total_h:.1f}" viewBox="0 0 {width} {total_h:.1f}">',
        f"<!-- {_esc(airport.get('name', ''))} "
        f"({_esc(airport.get('ident', ''))}) — rotation {rotation:.1f}deg CCW; "
        "north arrow shows true north on the rotated page; "
        "runway edges are geometry (plotter-safe), not stroke-width -->",
    ]
    counts = {"runway": 0, "taxiway": 0, "apron": 0, "terminal": 0,
              "hangar": 0, "stands": 0, "stopways": 0,
              **{cls: 0 for cls in CONTEXT_CLASSES}}

    # NOTE: no in-SVG title — name/country live as fixed labels at the top
    # of the /airports page (and the convert title block), never plotted.
    # NOTE: no frequency strip either — frequencies travel in the API
    # responses and page copy; the artwork is pure diagram geometry.

    # -- surrounding context first (faintest, under the airfield) ---------
    def clipped_d(world_poly: list[tuple[float, float]], close: bool) -> list[str]:
        """Map world → SVG, cut at the diagram frame, return path strings."""
        pts = [W2S(x, y) for x, y in world_poly]
        if close:
            ring = _clip_closed(pts)
            seqs = [ring] if len(ring) >= 3 else []
        else:
            seqs = _clip_open(pts)
        out = []
        for seq in seqs:
            d = f"M {seq[0][0]:.2f} {seq[0][1]:.2f} " + " ".join(
                f"L {x:.2f} {y:.2f}" for x, y in seq[1:]
            )
            if close:
                d += " Z"
            out.append(d)
        return out

    faint_w = max(0.5, width / 1600.0)
    for cls in CONTEXT_CLASSES:
        if cls not in selected:
            continue
        parts.append(
            f'<g id="context-{cls}" fill="none" stroke="#000000" '
            f'stroke-width="{faint_w:.2f}" stroke-linecap="round" stroke-linejoin="round">'
        )
        for poly in ctx_r.get(cls, []):
            for d in clipped_d(poly, cls not in ("highways", "roads", "paths", "rails")):
                parts.append(f"<path d=\"{d}\"/>")
                counts[cls] += 1
        parts.append("</g>")

    # -- OSM ground: taxiway centerlines; apron/terminal/hangar outlines ---
    for cls, gid, w, closed in (
        ("taxiway", "taxiways", taxi_w, False),
        ("apron", "aprons", thin_w, True),
        ("terminal", "terminals", thin_w, True),
        ("hangar", "hangars", thin_w, True),
        ("stands", "stands", thin_w, True),
        ("stopways", "stopways", thin_w, True),
    ):
        if cls not in selected:
            continue
        parts.append(
            f'<g id="osm-{gid}" fill="none" stroke="#000000" '
            f'stroke-width="{w:.2f}" stroke-linecap="round" stroke-linejoin="round">'
        )
        for poly in osm_r.get(cls, []):
            for d in clipped_d(poly, closed):
                parts.append(f"<path d=\"{d}\"/>")
                counts[cls] += 1
        parts.append("</g>")

    show_runway = "runway" in selected
    # -- OSM runway outlines (thin, under the authoritative strip) ----------
    if show_runway:
        parts.append(
            f'<g id="osm-runways" fill="none" stroke="#000000" '
            f'stroke-width="{thin_w:.2f}" stroke-linecap="round" stroke-linejoin="round">'
        )
        for poly in osm_r.get("runway", []):
            for d in clipped_d(poly, True):
                parts.append(f"<path d=\"{d}\"/>")
                counts["runway"] += 1
        parts.append("</g>")

    # -- authoritative strips: solid band (geometry-bold) ------------------
    # Nine longitudinal paths across 2× the true width. The exaggeration is
    # deliberate: at poster scale a true 45 m strip is ~1.6 mm wide and the
    # infill pitch (~0.3 mm) merges into a near-solid black bar — the
    # heaviest feature on the page. Pure geometry: stroke-width would not
    # survive the vector pipeline.
    if show_runway:
        parts.append(
            f'<g id="runways" fill="none" stroke="#000000" '
            f'stroke-width="{runway_w:.2f}" stroke-linecap="butt">'
        )
        for s in strips_r:
            dx, dy = s["he"][0] - s["le"][0], s["he"][1] - s["le"][1]
            seg = math.hypot(dx, dy) or 1.0
            nx, ny = -dy / seg, dx / seg  # world-space normal
            hw = s["width_m"] / 2.0 * _RUNWAY_WIDTH_EXAGGERATE
            strips_world = [
                [(s["le"][0] + nx * hw * frac, s["le"][1] + ny * hw * frac),
                 (s["he"][0] + nx * hw * frac, s["he"][1] + ny * hw * frac)]
                for frac in _RUNWAY_INFILL_FRACS
            ]
            for world_seg in strips_world:
                for d in clipped_d(world_seg, False):
                    parts.append(f"<path d=\"{d}\"/>")
                    counts["runway"] += 1
        parts.append("</g>")

    # -- taxiway designators (opt-in annotations of the taxiway layer) -----
    # One label per designator (OSM splits ways arbitrarily): keep the
    # longest surviving way so the ref sits on the major stretch. A ref
    # draws iff its way survives the min-detail filter, the taxiway layer
    # is selected, and the midpoint lands in frame.
    if taxiway_labels and "taxiway" in selected and taxiway_refs:
        best: dict[str, tuple[float, list[tuple[float, float]]]] = {}
        for ref, lonlat in taxiway_refs:
            kept = _project_polys([lonlat], closed=False)
            if not kept:
                continue
            poly = kept[0]
            length = sum(
                math.hypot(poly[i][0] - poly[i - 1][0], poly[i][1] - poly[i - 1][1])
                for i in range(1, len(poly))
            )
            if ref not in best or length > best[ref][0]:
                best[ref] = (length, poly)
        if best:
            parts.append('<g id="taxiway-labels" fill="none">')
            for ref in sorted(best):
                mx, my = W2S(*best[ref][1][len(best[ref][1]) // 2])
                if not _in_frame(mx, my):
                    continue
                parts.append(
                    f'<text x="{mx:.2f}" y="{my:.2f}" text-anchor="middle" '
                    f'font-family="monospace" data-stroke-font="hershey" font-size="{text_h:.1f}" '
                    f'stroke="none" fill="#000000">{_esc(ref)}</text>'
                )
            parts.append("</g>")

    # -- badges (ident) + displaced-threshold ticks -------------------------
    # Annotations of the runway layer: hidden with it. (No degree ovals —
    # the ident already encodes the heading; the oval was chart clutter.)
    if show_runway:
        parts.append(
            f'<g id="runway-marks" fill="none" stroke="#000000" '
            f'stroke-width="{thin_w:.2f}">'
        )
    for s in (strips_r if show_runway else []):
        x1, y1 = W2S(*s["le"])
        x2, y2 = W2S(*s["he"])
        dx, dy = x2 - x1, y2 - y1
        seg = math.hypot(dx, dy) or 1.0
        ux, uy = dx / seg, dy / seg
        nx, ny = -uy, ux
        half_band = (s["width_m"] * _RUNWAY_WIDTH_EXAGGERATE * scale) / 2.0
        for (px, py), ident, disp_m, end_sign in (
            ((x1, y1), s["le_ident"], s["le_disp_m"], -1.0),
            ((x2, y2), s["he_ident"], s["he_disp_m"], +1.0),
        ):
            # Off-frame runway ends (zoomed in) lose their furniture too.
            if not _in_frame(px, py):
                continue
            # Displaced-threshold tick: perpendicular bar disp_m inside the end.
            if disp_m > 0 and s["length_m"] > 0:
                frac = disp_m / s["length_m"]
                tx = px - ux * end_sign * frac * seg
                ty = py - uy * end_sign * frac * seg
                tick = _clip_segment(
                    (tx - nx * half_band, ty - ny * half_band),
                    (tx + nx * half_band, ty + ny * half_band),
                )
                if tick is not None:
                    (ax_, ay_), (bx_, by_) = tick
                    parts.append(
                        f"<path d=\"M {ax_:.2f} {ay_:.2f} L {bx_:.2f} {by_:.2f}\"/>"
                    )
            # Ident label just past the threshold.
            off_ident = half_band + text_h * 1.2
            ix, iy = px + ux * end_sign * off_ident, py + uy * end_sign * off_ident
            parts.append(
                f'<text x="{ix:.2f}" y="{iy + text_h * 0.35:.2f}" text-anchor="middle" '
                f'font-family="monospace" data-stroke-font="hershey" font-size="{text_h * 1.4:.1f}" '
                f'stroke="none" fill="#000000">{_esc(ident)}</text>'
            )
    if show_runway:
        parts.append("</g>")

    # -- compass: true north rotated by the SAME scene rotation -------------
    # World north (0,1) through rotate_point => consistent by construction.
    north = rotate_point(0.0, 1.0, rotation)
    ax = _MARGIN + diagram_w - text_h * 3.0
    ay = origin_y + text_h * 4.0
    arrow_len = text_h * 3.2
    # World→SVG flips y; apply the same flip to the rotated north vector.
    ex = ax + north[0] * arrow_len
    ey = ay - north[1] * arrow_len
    px_, py_ = -north[1], -north[0]  # perpendicular in SVG space
    parts.append(
        f'<g id="compass" fill="none" stroke="#000000" stroke-width="{taxi_w:.2f}">'
        f"<circle cx=\"{ax:.2f}\" cy=\"{ay:.2f}\" r=\"{arrow_len * 1.25:.2f}\"/>"
        f"<path d=\"M {ax:.2f} {ay:.2f} L {ex:.2f} {ey:.2f}\"/>"
        f"<path d=\"M {ex:.2f} {ey:.2f} "
        f"L {ex - north[0] * text_h * 0.9 + px_ * text_h * 0.45:.2f} "
        f"{ey + north[1] * text_h * 0.9 + py_ * text_h * 0.45:.2f} "
        f"L {ex - north[0] * text_h * 0.9 - px_ * text_h * 0.45:.2f} "
        f"{ey + north[1] * text_h * 0.9 - py_ * text_h * 0.45:.2f} Z\"/>"
        f'<text x="{ex + north[0] * text_h:.2f}" y="{ey - north[1] * text_h + text_h * 0.35:.2f}" '
        f'text-anchor="middle" font-family="monospace" data-stroke-font="hershey" font-size="{text_h:.1f}" '
        f'stroke="none" fill="#000000">N</text>'
        "</g>"
    )

    parts.append("</svg>")
    return "\n".join(parts) + "\n", counts, rotation, warnings


__all__ = ["render_diagram"]
