"""Integration: full render_diagram() on mock airfield data, then analyse the
emitted SVG itself — no label's ink box may touch any drawn line, and runway
idents keep a gap to the strip end (/egyedi-terv/repuloter spacing)."""

from __future__ import annotations

import math
import re

from backend.airports.overpass import CONTEXT_CLASSES
from backend.airports.render import render_diagram
from backend.airports.schemas import AIRFIELD_LAYERS
from backend.penplot.svg_vector import parse_svg_vectors

LAT0, LON0 = 47.4369, 19.2556
M_LAT = 1 / 111_000.0
M_LON = 1 / (111_000.0 * math.cos(math.radians(LAT0)))
LE = (19.2510, 47.4395)
HE = (19.2602, 47.4342)


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


TEXT_RE = re.compile(r"<text\b[^>]*>[^<]*</text>")


def _strokes(svg_body, head):
    """Real plotter strokes (Hershey text included) for an SVG fragment."""
    polys, _, _, _ = parse_svg_vectors((head + svg_body + "</svg>").encode())
    return polys


def _analyse(svg):
    """(geometry segments, {label: its single-stroke segments}) from the SVG
    text itself, run through the same vectorizer the plotter pipeline uses."""
    head = svg[: svg.index(">") + 1]
    geometry = TEXT_RE.sub("", svg)
    geo_polys, _, _, _ = parse_svg_vectors(geometry.encode())
    geo = [seg for poly in geo_polys for seg in zip(poly, poly[1:])]
    labels = {}
    for t in TEXT_RE.findall(svg):
        name = re.search(r">([^<]*)<", t).group(1)
        polys = _strokes(t, head)
        labels[name] = [seg for poly in polys for seg in zip(poly, poly[1:])]
    return geo, labels


def _seg_dist(a, b):
    """Min distance between two segments (touching/crossing → 0)."""
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    (p1, p2), (p3, p4) = a, b
    if cross(p1, p2, p3) * cross(p1, p2, p4) < 0 and cross(p3, p4, p1) * cross(p3, p4, p2) < 0:
        return 0.0

    def pt_seg(pt, s0, s1):
        dx, dy = s1[0] - s0[0], s1[1] - s0[1]
        n = dx * dx + dy * dy
        t = 0.0 if n == 0 else max(0.0, min(1.0, ((pt[0] - s0[0]) * dx + (pt[1] - s0[1]) * dy) / n))
        return math.hypot(pt[0] - (s0[0] + t * dx), pt[1] - (s0[1] + t * dy))

    return min(pt_seg(p1, *b), pt_seg(p2, *b), pt_seg(p3, *a), pt_seg(p4, *a))


def _clearance(label_segs, geo):
    return min(_seg_dist(l, g) for l in label_segs for g in geo)


def _bbox(segs):
    xs = [x for s in segs for x, _ in s]
    ys = [y for s in segs for _, y in s]
    return min(xs), min(ys), max(xs), max(ys)


def test_no_label_touches_any_drawn_line():
    svg, _, _, _ = _mock_render()
    geo, labels = _analyse(svg)
    assert {"13L", "31R", "B5", "C1"} <= set(labels), set(labels)
    for name, segs in labels.items():
        # Pen-width scale paper gap between glyph strokes and any drawn line.
        assert _clearance(segs, geo) >= 2.0, f"{name} is {_clearance(segs, geo):.2f} units from a drawn line"


def test_labels_do_not_overlap_each_other():
    svg, _, _, _ = _mock_render()
    _, labels = _analyse(svg)
    names = sorted(labels)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert _clearance(labels[a], labels[b]) >= 2.0, (a, b)


def test_runway_ident_gap_to_strip_end():
    svg, _, _, _ = _mock_render()
    geo, labels = _analyse(svg)
    strip = svg.split('id="runways"')[1].split("</g>")[0]
    paths = re.findall(r'<path d="M ([\d.]+) ([\d.]+) L ([\d.]+) ([\d.]+)"/>', strip)
    xs = [float(p[0]) for p in paths] + [float(p[2]) for p in paths]
    ys = [float(p[1]) for p in paths] + [float(p[3]) for p in paths]
    band = [((min(xs), min(ys)), (max(xs), min(ys))), ((min(xs), min(ys)), (min(xs), max(ys))),
            ((max(xs), min(ys)), (max(xs), max(ys))), ((min(xs), max(ys)), (max(xs), max(ys)))]
    for name in ("13L", "31R"):
        size = float(re.search(r'font-size="([\d.]+)"[^>]*>%s<' % name, svg).group(1))
        # At least a glyph's worth of paper between runway band and ident ink.
        assert _clearance(labels[name], band) >= size * 0.5, name


def test_dense_clutter_labels_get_a_white_box():
    """Airport-independent: even where a mesh of roads covers every possible
    spot, the labels stay readable — lines are cut out of a padded box around
    each label (a 'white background'), and only there."""
    mesh = []
    for i in range(-40, 41):
        mesh.append([_offset(LE, -300, i * 12.0), _offset(HE, 300, i * 12.0)])  # along
    for j in range(-30, 120):
        mesh.append([_offset(LE, j * 12.0 - 400, -480), _offset(LE, j * 12.0 - 400, 480)])  # across
    base = {"runway": [], "taxiway": [[_offset(LE, -250, 250), _offset(HE, 250, 250)]],
            "apron": [], "terminal": [], "hangar": []}
    kw = dict(airport=_airport(), runways=[_runway()], frequencies=[], osm_geoms=base,
              width=1000, layers=[*AIRFIELD_LAYERS, *CONTEXT_CLASSES],
              taxiway_refs=[("B5", [_offset(LE, 150, -60), _offset(LE, 600, -60)])],
              taxiway_labels=True)
    plain, _, _, _ = render_diagram(**kw, context_geoms={"roads": []})
    svg, _, _, _ = render_diagram(**kw, context_geoms={"roads": mesh})
    geo, labels = _analyse(svg)
    pad = 1000 * 2.5 / 210.0
    assert {"13L", "31R", "B5"} <= set(labels)
    strip = svg.split('id="runways"')[1].split("</g>")[0]
    n_strip = len(re.findall(r"<path", strip))
    assert n_strip == len(re.findall(r"<path", plain.split('id="runways"')[1].split("</g>")[0]))
    for name, segs in labels.items():
        # Every road piece stays >= ~pad away from the glyph strokes (box cut),
        # runway strip excepted (never cut; the ident keeps its own gap).
        road_geo = [g for g in geo if g not in _runway_segs(svg)]
        assert _clearance(segs, road_geo) >= pad * 0.9, name
    # ...and the mesh is still there away from the labels.
    road_group = svg.split('id="context-roads"')[1].split("</g>")[0]
    assert road_group.count("<path") > 50


def _runway_segs(svg):
    strip = svg.split('id="runways"')[1].split("</g>")[0]
    out = set()
    for m in re.findall(r'<path d="M ([\d.]+) ([\d.]+) L ([\d.]+) ([\d.]+)"/>', strip):
        out.add(((float(m[0]), float(m[1])), (float(m[2]), float(m[3]))))
    return out


def test_apron_lane_refs_are_not_labelled():
    """Gate-like refs (P1, H2…) on ways lying inside an apron outline are apron
    lanes, not taxiways: no label. A real taxiway crossing/outside keeps its ref;
    a ref that also has a way outside the apron is labelled on that way."""
    apron = [_offset(LE, 100, -150), _offset(LE, 100, 150), _offset(LE, 500, 150),
             _offset(LE, 500, -150), _offset(LE, 100, -150)]
    lane = [_offset(LE, 200, -100), _offset(LE, 400, -100)]        # fully inside
    lane2 = [_offset(LE, 220, 60), _offset(LE, 420, 60)]           # fully inside
    twy = [_offset(LE, -200, -400), _offset(LE, 700, -400)]        # outside
    mixed_in = [_offset(LE, 250, 0), _offset(LE, 350, 0)]          # inside…
    mixed_out = [_offset(LE, 600, -300), _offset(LE, 900, -300)]   # …and outside
    osm = {"runway": [], "taxiway": [lane, lane2, twy, mixed_in, mixed_out],
           "apron": [apron], "terminal": [], "hangar": []}
    refs = [("P1", lane), ("H2", lane2), ("B5", twy), ("A1", mixed_in), ("A1", mixed_out)]
    svg, _, _, _ = render_diagram(
        airport=_airport(), runways=[_runway()], frequencies=[], osm_geoms=osm, width=1000,
        layers=["taxiway", "apron"], taxiway_refs=refs, taxiway_labels=True,
    )
    _, labels = _analyse(svg)
    names = set(labels) - {"13L", "31R"}
    assert names == {"B5", "A1"}, names
