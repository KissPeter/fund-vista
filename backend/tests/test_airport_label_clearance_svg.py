"""Integration: full render_diagram() on mock airfield data, then analyse the
emitted SVG itself — no label's ink box may touch any drawn line, and runway
idents keep a gap to the strip end (/egyedi-terv/repuloter spacing)."""

from __future__ import annotations

import math
import re

from backend.airports.overpass import CONTEXT_CLASSES
from backend.airports.render import render_diagram
from backend.airports.schemas import AIRFIELD_LAYERS

LAT0, LON0 = 47.4369, 19.2556
M_LAT = 1 / 111_000.0
M_LON = 1 / (111_000.0 * math.cos(math.radians(LAT0)))
LE = (19.2510, 47.4395)
HE = (19.2602, 47.4342)
CHAR_W, HALF_H, PAD = 0.62, 0.40, 0.25  # same label model as the renderer


def _airport():
    return {"ident": "LHBP", "name": "Mock", "latitude_deg": LAT0, "longitude_deg": LON0}


def _runway():
    return {
        "le_ident": "13L", "he_ident": "31R", "length_ft": "3000", "width_ft": "150",
        "surface": "ASP",
        "le_latitude_deg": str(LE[1]), "le_longitude_deg": str(LE[0]),
        "he_latitude_deg": str(HE[1]), "he_longitude_deg": str(HE[0]),
        "le_displaced_threshold_ft": "0", "he_displaced_threshold_ft": "0",
    }


def _offset(p, along_m, across_m):
    """Point `along_m` past p on the LE→HE axis (+ across), as lon/lat."""
    dx = (HE[0] - LE[0]) / M_LON
    dy = (HE[1] - LE[1]) / M_LAT
    n = math.hypot(dx, dy)
    ux, uy = dx / n, dy / n
    return (p[0] + (ux * along_m - uy * across_m) * M_LON,
            p[1] + (uy * along_m + ux * across_m) * M_LAT)


def _mock_render():
    # Roads crossing right where the idents would sit by default.
    road_le = [_offset(LE, -22, -300), _offset(LE, -22, 300)]
    road_he = [_offset(HE, 22, -300), _offset(HE, 22, 300)]
    # Taxiway B5 parallel to the runway, close on its left; a second one (C1)
    # crossing it so the label has to dodge both routes.
    b5 = [_offset(LE, 150, -60), _offset(LE, 600, -60)]
    c1 = [_offset(LE, 380, -200), _offset(LE, 380, 40)]
    # Long outer taxiway (no label) so the frame has margin beyond both ends.
    outer = [_offset(LE, -250, 250), _offset(HE, 250, 250)]
    osm = {"runway": [], "taxiway": [b5, c1, outer], "apron": [], "terminal": [], "hangar": []}
    ctx = {"roads": [road_le, road_he]}
    refs = [("B5", b5), ("C1", c1)]
    return render_diagram(
        airport=_airport(), runways=[_runway()], frequencies=[], osm_geoms=osm,
        context_geoms=ctx, width=1000,
        layers=[*AIRFIELD_LAYERS, *CONTEXT_CLASSES], taxiway_refs=refs, taxiway_labels=True,
    )


def _parse(svg):
    """(segments of every drawn path, [(text, cx, cy, size)])."""
    segs = []
    for d in re.findall(r'<path d="([^"]+)"', svg):
        pts = [(float(x), float(y)) for x, y in re.findall(r"(-?[\d.]+) (-?[\d.]+)", d)]
        segs += list(zip(pts, pts[1:]))
        if d.rstrip().endswith("Z"):
            segs.append((pts[-1], pts[0]))
    labels = []
    for x, y, size, text in re.findall(
        r'<text x="(-?[\d.]+)" y="(-?[\d.]+)"[^>]*font-size="([\d.]+)"[^>]*>([^<]+)</text>', svg
    ):
        size = float(size)
        labels.append((text, float(x), float(y) - size * 0.35, size))  # baseline → centre
    return segs, labels


def _hits(seg, box):
    (x0, y0), (x1, y1) = seg
    xmin, ymin, xmax, ymax = box
    t0, t1 = 0.0, 1.0
    for p, q in ((-(x1 - x0), x0 - xmin), (x1 - x0, xmax - x0),
                 (-(y1 - y0), y0 - ymin), (y1 - y0, ymax - y0)):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def _box(text, cx, cy, size, pad=0.0):
    hw = len(text) * size * CHAR_W / 2 + pad
    hh = size * HALF_H + pad
    return (cx - hw, cy - hh, cx + hw, cy + hh)


def test_no_label_touches_any_drawn_line():
    svg, _, _, _ = _mock_render()
    segs, labels = _parse(svg)
    texts = {t for t, *_ in labels}
    assert {"13L", "31R", "B5", "C1"} <= texts, texts
    for text, cx, cy, size in labels:
        box = _box(text, cx, cy, size, pad=size * 0.1)  # very minor padding
        crossed = [s for s in segs if _hits(s, box)]
        assert not crossed, f"{text} ink box overlaps {len(crossed)} drawn segment(s): {crossed[:2]}"


def test_labels_do_not_overlap_each_other():
    svg, _, _, _ = _mock_render()
    _, labels = _parse(svg)
    boxes = [(t, _box(t, cx, cy, s)) for t, cx, cy, s in labels]
    for i, (ta, a) in enumerate(boxes):
        for tb, b in boxes[i + 1:]:
            assert a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1], (ta, tb)


def test_runway_ident_gap_to_strip_end():
    svg, _, _, _ = _mock_render()
    _, labels = _parse(svg)
    strip = svg.split('id="runways"')[1].split("</g>")[0]
    paths = re.findall(r'<path d="M ([\d.]+) ([\d.]+) L ([\d.]+) ([\d.]+)"/>', strip)
    centre = paths[len(paths) // 2]  # centerline infill path
    ends = [(float(centre[0]), float(centre[1])), (float(centre[2]), float(centre[3]))]
    idents = [(t, cx, cy, s) for t, cx, cy, s in labels if t in ("13L", "31R")]
    assert len(idents) == 2
    for text, cx, cy, size in idents:
        gap = min(math.hypot(cx - ex, cy - ey) for ex, ey in ends) - size * HALF_H
        assert gap >= size * 0.5, f"{text}: only {gap:.1f} units between strip end and ink"
