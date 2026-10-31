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
    spot, the runway idents stay readable — lines are cut out of a padded box
    around each (a 'white background'), and only there."""
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
    # Runway idents are never dropped; the taxiway ref finds no clear paper in
    # a mesh this dense and is decluttered instead of being forced.
    assert {"13L", "31R"} <= set(labels) and "B5" not in labels, set(labels)
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


def _taxi_render(osm_taxiways, refs, *, ctx=None, extra_osm=None, **kw):
    """Render taxiways (+optional context roads) with labels on; return svg."""
    osm = {"runway": [], "taxiway": osm_taxiways, "apron": [], "terminal": [], "hangar": []}
    osm.update(extra_osm or {})
    svg, _, _, _ = render_diagram(
        airport=_airport(), runways=[_runway()], frequencies=[], osm_geoms=osm,
        context_geoms=ctx, width=1000, layers=kw.pop("layers", [*AIRFIELD_LAYERS, *CONTEXT_CLASSES]),
        taxiway_refs=refs, taxiway_labels=True, **kw,
    )
    return svg


def _frame_of(svg):
    m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    return float(m.group(1)), float(m.group(2))


def _outer():
    # Long outer taxiway (unlabelled: no ref entry) so runway ends have frame margin.
    return [_offset(LE, -250, 250), _offset(HE, 250, 250)]


def test_open_space_labels_all_drawn_with_clear_paper():
    ways = {"B1": [_offset(LE, 150, -60), _offset(LE, 700, -60)],
            "C2": [_offset(LE, 380, -250), _offset(LE, 380, -80)],
            "D3": [_offset(LE, 100, 120), _offset(LE, 600, 120)]}
    svg = _taxi_render([*ways.values(), _outer()], list(ways.items()))
    geo, labels = _analyse(svg)
    pad = 1000 * 2.5 / 210.0
    assert set(ways) <= set(labels), set(labels)
    for ref in ways:
        # Taxiway refs are only ever placed on clear paper: >= half the pad,
        # never needing a knock-out, from every drawn line (runway included).
        assert _clearance(labels[ref], geo) >= pad * 0.45, ref
    w, h = _frame_of(svg)
    for name, segs in labels.items():
        x0, y0, x1, y1 = _bbox(segs)
        assert x0 >= 0 and y0 >= 0 and x1 <= w and y1 <= h, name


def test_cluttered_refs_are_dropped_not_forced():
    """A ref whose every candidate spot would cross a line (apron-lane clutter)
    is decluttered; the clear taxiway keeps its label."""
    clear = [_offset(LE, 150, -60), _offset(LE, 700, -60)]
    lane = [_offset(LE, 300, 100), _offset(LE, 500, 100)]
    # Closely spaced parallel lanes (a stand field) all around the lane.
    field = [[_offset(LE, 250, 20 + i * 6.0), _offset(LE, 560, 20 + i * 6.0)] for i in range(0, 60)]
    svg = _taxi_render([clear, lane, *field, _outer()], [("B1", clear), ("P1", lane)])
    _, labels = _analyse(svg)
    assert "B1" in labels
    assert "P1" not in labels
    # No knock-out scar either: a dropped label leaves the lane field intact.
    field_only = _taxi_render([clear, lane, *field, _outer()], [("B1", clear)])
    assert svg.count("<path") == field_only.count("<path")


def test_isolated_apron_lane_keeps_its_label():
    """The overlay check is per-label, not a blanket rule on 'apron-looking'
    refs: a lane with clear paper around it is still labelled."""
    lane = [_offset(LE, 300, 100), _offset(LE, 500, 100)]
    apron = [_offset(LE, 200, 0), _offset(LE, 200, 250), _offset(LE, 600, 250),
             _offset(LE, 600, 0), _offset(LE, 200, 0)]
    svg = _taxi_render([lane, _outer()], [("P1", lane)], extra_osm={"apron": [apron]})
    _, labels = _analyse(svg)
    assert "P1" in labels


ROUTE_Y = -200  # metres off the runway axis: free paper on both sides of the route


def _pocket_walls(gap_lo, gap_hi, step=5):
    """Cross-lines filling both sides of a route except one 'pocket' beside it."""
    walls = []
    for a in range(150, 701, step):
        walls.append([_offset(LE, a, ROUTE_Y - 3), _offset(LE, a, ROUTE_Y - 160)])
        if not gap_lo <= a <= gap_hi:
            walls.append([_offset(LE, a, ROUTE_Y + 3), _offset(LE, a, ROUTE_Y + 120)])
    return walls

def test_label_priority_is_longest_route_then_ref():
    from backend.airports.render import _label_order

    assert _label_order({"S9": 40.0, "L1": 500.0, "M2": 40.0, "A1": 200.0}) == ["L1", "A1", "M2", "S9"]
    assert _label_order({"B": 1.0, "A": 1.0}) == ["A", "B"]


def test_contested_pocket_gets_exactly_one_label_deterministically():
    """Two designators on the same route share the only free pocket: exactly
    one is drawn (never both stacked, never a forced overlap), the same one
    whatever order the refs arrive in."""
    way = [_offset(LE, 150, ROUTE_Y), _offset(LE, 700, ROUTE_Y)]
    geo = [way, *_pocket_walls(385, 465), _outer()]
    runs = [
        _taxi_render(geo, [("L3", way), ("L2", way)]),
        _taxi_render(geo, [("L2", way), ("L3", way)]),
    ]
    assert runs[0] == runs[1]
    _, labels = _analyse(runs[0])
    drawn = set(labels) - {"13L", "31R"}
    assert drawn == {"L2"}, drawn  # tie on length -> the smaller ref, once
    geo_segs, _ = _analyse(runs[0])
    assert _clearance(labels["L2"], geo_segs) >= 1000 * 2.5 / 210.0 * 0.45

def test_longer_route_beats_a_shorter_one_for_the_only_pocket():
    long_way = [_offset(LE, 150, ROUTE_Y), _offset(LE, 700, ROUTE_Y)]
    short_way = [_offset(LE, 300, ROUTE_Y), _offset(LE, 550, ROUTE_Y)]
    geo = [long_way, short_way, *_pocket_walls(385, 465), _outer()]
    for refs in ([("L3", long_way), ("L2", short_way)], [("L2", short_way), ("L3", long_way)]):
        _, labels = _analyse(_taxi_render(geo, refs))
        # L2 would win the tie-break by name; L3 wins because its route is longer.
        assert set(labels) - {"13L", "31R"} == {"L3"}, set(labels)


def test_runway_idents_always_drawn_even_in_total_clutter():
    mesh = [[_offset(LE, -400, i * 5.0), _offset(HE, 400, i * 5.0)] for i in range(-120, 121)]
    mesh += [[_offset(LE, j * 5.0 - 400, -600), _offset(LE, j * 5.0 - 400, 600)] for j in range(0, 400)]
    svg = _taxi_render([_outer()], [], ctx={"roads": mesh})
    _, labels = _analyse(svg)
    assert {"13L", "31R"} <= set(labels)

