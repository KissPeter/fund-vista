"""Blueprint-style SVG rendering for airport ground diagrams.

Layout (client-approved reference): title header, column frequency strip
with rule, diagram area, footer — all strokes, ``fill="none"`` (except the
arrowhead marker, structurally needed). Stays black-on-paper: the plotter
draws lines, not fills — the white-on-navy look of the reference mock is a
display style, available via the page's Display background picker.

Runway boldness is GEOMETRY, not ``stroke-width``: each strip is drawn as
two edge paths (± half ``width_ft``) plus a centerline, so the runway stays
bold through ``parse_svg_vectors`` (which discards stroke widths), vpype and
the single-ink plot. The heavy ``stroke-width`` on the group only affects
the raw-SVG preview.

Text stays as ``<text>`` (monospace): Inkscape converts it to paths before
plotting via the iDraw extension — same step that already handles title
blocks for the other product lines.
"""

from __future__ import annotations

import math
from xml.sax.saxutils import escape as _xml_escape

from backend.airports.geometry import (
    project,
    rotate_point,
    rotation_for_heading,
    runway_heading_deg,
)
from backend.airports.ourairports import FT_TO_M, heading_from_ident

# A4 portrait in user units at 1000 wide → height set by content; the plotter
# scales the viewBox to the page with margin (iDraw working area 210×297mm).
_MARGIN = 40.0
_TITLE_H = 46.0
_STRIP_H = 96.0
_STRIP_PAD = 14.0
_FOOTER_H = 54.0
_MAX_FREQ_COLS = 5


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
) -> tuple[str, dict[str, int], float, list[str]]:
    """Render the full blueprint SVG.

    Returns ``(svg_text, path_counts, rotation_deg, warnings)``. ``airport``
    needs ``latitude_deg/longitude_deg`` (+ name/elevation/idents for the
    header/footer). ``runways`` are raw ``runways.csv`` rows; endpoints come
    from :func:`runway_endpoints` (authoritative coords or ident-heading
    fallback).
    """
    from backend.airports.ourairports import runway_endpoints

    warnings: list[str] = []
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
    ) -> list[list[tuple[float, float]]]:
        kept: list[list[tuple[float, float]]] = []
        for lonlat in polys:
            pts = [proj(ll) for ll in lonlat]
            length = sum(
                math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
                for i in range(1, len(pts))
            )
            if length < min_path_len_m:
                continue
            kept.append([rotate_point(x, y, rotation) for x, y in pts])
        return kept

    osm_r: dict[str, list[list[tuple[float, float]]]] = {}
    for cls, polys in osm_geoms.items():
        osm_r[cls] = _project_polys(polys)
    if not any(osm_r.values()):
        warnings.append("no_osm_aeroway")
    ctx_r: dict[str, list[list[tuple[float, float]]]] = {}
    if context_geoms:
        for cls, polys in context_geoms.items():
            ctx_r[cls] = _project_polys(polys)
        if not any(ctx_r.values()):
            warnings.append("no_context_data")

    # -- fit rotated world → diagram area ---------------------------------
    all_pts: list[tuple[float, float]] = []
    for s in strips_r:
        all_pts += [s["le"], s["he"]]
    for polys in list(osm_r.values()) + list(ctx_r.values()):
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

    header_h = _TITLE_H + _STRIP_H
    diagram_w = width - 2 * _MARGIN
    scale = diagram_w / max(max_x - min_x, 1e-9)
    diagram_h = (max_y - min_y) * scale
    total_h = _MARGIN + header_h + _MARGIN / 2 + diagram_h + _MARGIN / 2 + _FOOTER_H + _MARGIN
    origin_y = _MARGIN + header_h + _MARGIN / 2

    def W2S(x: float, y: float) -> tuple[float, float]:
        # World (+x east, +y north) → SVG (+x right, +y down).
        return (_MARGIN + (x - min_x) * scale, origin_y + (max_y - y) * scale)

    # -- stroke weights (preview only; hierarchy survives via geometry) ----
    runway_w = max(6.0, width / 130.0)
    taxi_w = max(1.2, width / 700.0)
    thin_w = max(0.8, width / 1100.0)
    title_h = max(22.0, width / 34.0)
    freq_big = max(20.0, width / 42.0)
    freq_small = max(11.0, width / 72.0)
    text_h = max(11.0, width / 72.0)

    place = (airport.get("municipality") or airport.get("name") or "").strip()
    title = f"{place}, {airport.get('iso_country', '')}".strip(" ,").upper()
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{total_h:.1f}" viewBox="0 0 {width} {total_h:.1f}">',
        f"<!-- {_esc(airport.get('name', ''))} "
        f"({_esc(airport.get('ident', ''))}) — rotation {rotation:.1f}deg CCW; "
        "north arrow shows true north on the rotated page; "
        "runway edges are geometry (plotter-safe), not stroke-width -->",
    ]
    counts = {"runway": 0, "taxiway": 0, "apron": 0, "terminal": 0,
              "hangar": 0, "stands": 0, "stopways": 0, "context": 0}

    # -- title header --------------------------------------------------------
    title_y = _MARGIN + _TITLE_H * 0.7
    parts.append(
        f'<g id="title" fill="none" stroke="#000000" stroke-width="{thin_w:.2f}">'
        f'<text x="{width / 2:.1f}" y="{title_y:.1f}" text-anchor="middle" '
        f'font-family="monospace" font-size="{title_h:.1f}" letter-spacing="4" '
        f'stroke="none" fill="#000000">{_esc(title)}</text>'
        "</g>"
    )

    # -- frequency strip: columns (label small, frequency large) -------------
    strip_y = _MARGIN + _TITLE_H
    cols = frequencies[:_MAX_FREQ_COLS]
    ncols = max(len(cols), 1)
    parts.append(
        f'<g id="freq-strip" fill="none" stroke="#000000" stroke-width="{thin_w:.2f}">'
        f'<rect x="{_MARGIN:.1f}" y="{strip_y:.1f}" width="{diagram_w:.1f}" '
        f'height="{_STRIP_H:.1f}"/>'
    )
    if cols:
        for i, freq in enumerate(cols):
            cx = _MARGIN + diagram_w * (i + 0.5) / ncols
            if i > 0:
                div_x = _MARGIN + diagram_w * i / ncols
                parts.append(
                    f"<path d=\"M {div_x:.1f} {strip_y + 8:.1f} "
                    f"L {div_x:.1f} {strip_y + _STRIP_H - 8:.1f}\"/>"
                )
            label = f"{freq['type']} {freq['description']}".strip().upper()
            parts.append(
                f'<text x="{cx:.1f}" y="{strip_y + 30:.1f}" text-anchor="middle" '
                f'font-family="monospace" font-size="{freq_small:.1f}" '
                f'stroke="none" fill="#000000">{_esc(label)}</text>'
                f'<text x="{cx:.1f}" y="{strip_y + 30 + freq_big + 8:.1f}" text-anchor="middle" '
                f'font-family="monospace" font-size="{freq_big:.1f}" '
                f'stroke="none" fill="#000000">{freq["frequency_mhz"]:.3f}</text>'
            )
        if len(frequencies) > _MAX_FREQ_COLS:
            parts.append(
                f'<text x="{_MARGIN + diagram_w - 8:.1f}" y="{strip_y + _STRIP_H - 10:.1f}" '
                f'text-anchor="end" font-family="monospace" font-size="{freq_small:.1f}" '
                f'stroke="none" fill="#000000">+{len(frequencies) - _MAX_FREQ_COLS} more</text>'
            )
    else:
        parts.append(
            f'<text x="{width / 2:.1f}" y="{strip_y + _STRIP_H / 2 + freq_small / 2:.1f}" '
            f'text-anchor="middle" font-family="monospace" font-size="{freq_small:.1f}" '
            f'stroke="none" fill="#000000">No published frequencies</text>'
        )
        warnings.append("no_frequencies")
    rule_y = strip_y + _STRIP_H
    parts.append(
        f"<path d=\"M {_MARGIN:.1f} {rule_y:.1f} "
        f"L {_MARGIN + diagram_w:.1f} {rule_y:.1f}\"/>"
    )
    parts.append("</g>")

    # -- surrounding context first (faintest, under the airfield) ---------
    def path_d(poly: list[tuple[float, float]], close: bool) -> str:
        pts = [W2S(x, y) for x, y in poly]
        d = f"M {pts[0][0]:.2f} {pts[0][1]:.2f} " + " ".join(
            f"L {x:.2f} {y:.2f}" for x, y in pts[1:]
        )
        if close and len(pts) > 2:
            first, last = pts[0], pts[-1]
            if math.hypot(first[0] - last[0], first[1] - last[1]) < 1e-6:
                d += " Z"
        return d

    faint_w = max(0.5, width / 1600.0)
    parts.append(
        f'<g id="context" fill="none" stroke="#000000" '
        f'stroke-width="{faint_w:.2f}" stroke-linecap="round" stroke-linejoin="round">'
    )
    for cls in ("roads", "buildings", "water"):
        for poly in ctx_r.get(cls, []):
            parts.append(f"<path d=\"{path_d(poly, cls != 'roads')}\"/>")
            counts["context"] += 1
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
        parts.append(
            f'<g id="osm-{gid}" fill="none" stroke="#000000" '
            f'stroke-width="{w:.2f}" stroke-linecap="round" stroke-linejoin="round">'
        )
        for poly in osm_r.get(cls, []):
            parts.append(f"<path d=\"{path_d(poly, closed)}\"/>")
            counts[cls] += 1
        parts.append("</g>")

    # -- OSM runway outlines (thin, under the authoritative strip) ----------
    parts.append(
        f'<g id="osm-runways" fill="none" stroke="#000000" '
        f'stroke-width="{thin_w:.2f}" stroke-linecap="round" stroke-linejoin="round">'
    )
    for poly in osm_r.get("runway", []):
        parts.append(f"<path d=\"{path_d(poly, True)}\"/>")
        counts["runway"] += 1
    parts.append("</g>")

    # -- authoritative strips: 2 edges + centerline (geometry-bold) ---------
    parts.append(
        f'<g id="runways" fill="none" stroke="#000000" '
        f'stroke-width="{runway_w:.2f}" stroke-linecap="butt">'
    )
    for s in strips_r:
        dx, dy = s["he"][0] - s["le"][0], s["he"][1] - s["le"][1]
        seg = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / seg, dx / seg  # world-space normal
        hw = s["width_m"] / 2.0
        for side in (-1.0, 1.0):
            edge = [(s["le"][0] + nx * hw * side, s["le"][1] + ny * hw * side),
                    (s["he"][0] + nx * hw * side, s["he"][1] + ny * hw * side)]
            x1, y1 = W2S(*edge[0])
            x2, y2 = W2S(*edge[1])
            parts.append(f"<path d=\"M {x1:.2f} {y1:.2f} L {x2:.2f} {y2:.2f}\"/>")
            counts["runway"] += 1
        x1, y1 = W2S(*s["le"])
        x2, y2 = W2S(*s["he"])
        parts.append(f"<path d=\"M {x1:.2f} {y1:.2f} L {x2:.2f} {y2:.2f}\"/>")
        counts["runway"] += 1
    parts.append("</g>")

    # -- badges (ident) + degree ovals + displaced-threshold ticks -----------
    parts.append(
        f'<g id="runway-marks" fill="none" stroke="#000000" '
        f'stroke-width="{thin_w:.2f}">'
    )
    for s in strips_r:
        x1, y1 = W2S(*s["le"])
        x2, y2 = W2S(*s["he"])
        dx, dy = x2 - x1, y2 - y1
        seg = math.hypot(dx, dy) or 1.0
        ux, uy = dx / seg, dy / seg
        nx, ny = -uy, ux
        half_band = (s["width_m"] * scale) / 2.0
        for (px, py), ident, deg, disp_m, end_sign in (
            ((x1, y1), s["le_ident"], s["le_deg"], s["le_disp_m"], -1.0),
            ((x2, y2), s["he_ident"], s["he_deg"], s["he_disp_m"], +1.0),
        ):
            # Displaced-threshold tick: perpendicular bar disp_m inside the end.
            if disp_m > 0 and s["length_m"] > 0:
                frac = disp_m / s["length_m"]
                tx = px - ux * end_sign * frac * seg
                ty = py - uy * end_sign * frac * seg
                parts.append(
                    f"<path d=\"M {tx - nx * half_band:.2f} {ty - ny * half_band:.2f} "
                    f"L {tx + nx * half_band:.2f} {ty + ny * half_band:.2f}\"/>"
                )
            # Ident label just past the threshold …
            off_ident = half_band + text_h * 1.2
            ix, iy = px + ux * end_sign * off_ident, py + uy * end_sign * off_ident
            parts.append(
                f'<text x="{ix:.2f}" y="{iy + text_h * 0.35:.2f}" text-anchor="middle" '
                f'font-family="monospace" font-size="{text_h * 1.4:.1f}" '
                f'stroke="none" fill="#000000">{_esc(ident)}</text>'
            )
            # … and the degree oval beyond it.
            off_deg = half_band + text_h * 3.6
            bx, by = px + ux * end_sign * off_deg, py + uy * end_sign * off_deg
            rx, ry = text_h * 1.9, text_h * 1.05
            deg_txt = f"{deg:.0f}°" if deg is not None else "?"
            parts.append(
                f'<ellipse cx="{bx:.2f}" cy="{by:.2f}" rx="{rx:.2f}" ry="{ry:.2f}"/>'
                f'<text x="{bx:.2f}" y="{by + text_h * 0.32:.2f}" text-anchor="middle" '
                f'font-family="monospace" font-size="{text_h * 0.9:.1f}" '
                f'stroke="none" fill="#000000">{_esc(deg_txt)}</text>'
            )
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
        f'text-anchor="middle" font-family="monospace" font-size="{text_h:.1f}" '
        f'stroke="none" fill="#000000">N</text>'
        "</g>"
    )

    # -- footer: elevation + idents left, data credit right --------------------
    fy = origin_y + diagram_h + _MARGIN / 2 + _FOOTER_H / 2
    footer_fs = max(9.0, width / 90.0)
    elev = airport.get("elevation_ft") or ""
    try:
        elev_txt = f"Elev. {float(elev):.0f}'" if str(elev).strip() else "elev unknown"
    except ValueError:
        elev_txt = "elev unknown"
    left = (
        f"{elev_txt} • {airport.get('ident', '')}"
        + (f" / {airport.get('iata_code', '')}" if (airport.get("iata_code") or "").strip() else "")
    )
    right = (
        "Runway/frequency data: OurAirports.com (CC0). "
        "Ground layout: © OpenStreetMap contributors (ODbL)."
    )
    parts.append(
        f'<g id="footer" fill="none" stroke="#000000" stroke-width="{thin_w:.2f}">'
        f'<text x="{_MARGIN:.1f}" y="{fy:.1f}" '
        f'font-family="monospace" font-size="{footer_fs:.1f}" '
        f'stroke="none" fill="#000000">{_esc(left)}</text>'
        f'<text x="{_MARGIN + diagram_w:.1f}" y="{fy:.1f}" text-anchor="end" '
        f'font-family="monospace" font-size="{footer_fs:.1f}" '
        f'stroke="none" fill="#000000">{_esc(right)}</text>'
        "</g>"
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n", counts, rotation, warnings


__all__ = ["render_diagram"]
