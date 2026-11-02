"""Integration: runway ink has priority. Print the map black with a red runway
and any taxiway/road/apron line under the runway band would be overprinted (the
red does not cover black). The emitted SVG must hold no other ink inside the
band; lines are cut back to a no gap: they meet the strip edge exactly."""

from __future__ import annotations

import math
import re

from backend.airports.overpass import CONTEXT_CLASSES
from backend.airports.render import render_diagram
from backend.airports.schemas import AIRFIELD_LAYERS
from backend.tests.test_airport_label_clearance_svg import LE, HE, _airport, _offset, _runway

FLUSH_TOL = 0.35  # units: cut ends sit on the strip edge, no gap
RUNWAY_INK = ("runways", "runway-marks", "osm-runways")


def _render(taxiways, *, roads=(), aprons=(), hangars=(), layers=None, runways=True):
    osm = {"runway": [], "taxiway": list(taxiways), "apron": list(aprons),
           "terminal": [], "hangar": list(hangars)}
    svg, _, _, _ = render_diagram(
        airport=_airport(), runways=[_runway()] if runways else [], frequencies=[], osm_geoms=osm,
        context_geoms={"roads": list(roads)}, width=1000,
        layers=layers or [*AIRFIELD_LAYERS, *CONTEXT_CLASSES],
    )
    return svg


def _groups(svg):
    """{group id: [list of polylines]} from the emitted SVG."""
    out, cur = {}, None
    for line in svg.splitlines():
        gm = re.match(r'<g id="([^"]+)"', line)
        if gm:
            cur = gm.group(1)
            out.setdefault(cur, [])
        pm = re.match(r'<path d="([^"]+)"', line)
        if pm and cur:
            out[cur].append([(float(x), float(y)) for x, y in re.findall(r"(-?[\d.]+) (-?[\d.]+)", pm.group(1))])
    return out


def _band(svg):
    """Runway strip quad (SVG space) from the first/last infill lines."""
    paths = _groups(svg)["runways"]
    a, b = paths[0], paths[-1]
    return [a[0], a[-1], b[-1], b[0]]


def _inside(pt, poly, inset=0.0):
    area = sum(p[0] * q[1] - q[0] * p[1] for p, q in zip(poly, poly[1:] + poly[:1]))
    sgn = 1.0 if area >= 0 else -1.0
    for p, q in zip(poly, poly[1:] + poly[:1]):
        ex, ey = q[0] - p[0], q[1] - p[1]
        cross = sgn * (ex * (pt[1] - p[1]) - ey * (pt[0] - p[0]))
        if cross <= inset * math.hypot(ex, ey):
            return False
    return True


def _samples(poly, step=0.5):
    for (x0, y0), (x1, y1) in zip(poly, poly[1:]):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
        for i in range(n + 1):
            t = i / n
            yield (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)


def _ink_inside_band(svg):
    """Samples of NON-runway ink that lie strictly inside the runway band."""
    band, bad = _band(svg), []
    for gid, polys in _groups(svg).items():
        if gid in RUNWAY_INK:
            continue
        for poly in polys:
            bad += [(gid, p) for p in _samples(poly) if _inside(p, band, inset=0.3)]
    return bad


def _mock():
    along = 400
    return dict(
        # taxiway crossing the runway at right angles, and one running along inside the band
        taxiways=[[_offset(LE, along, -250), _offset(LE, along, 250)],
                  [_offset(LE, 150, 12), _offset(LE, 700, 12)],
                  [_offset(LE, 100, -400), _offset(LE, 700, -400)]],      # far away, never touches
        roads=[[_offset(LE, 200, -300), _offset(LE, 600, 300)]],           # diagonal road across
        aprons=[[_offset(LE, 500, -100), _offset(LE, 500, 100), _offset(LE, 650, 100),
                 _offset(LE, 650, -100), _offset(LE, 500, -100)]],        # outline overlapping the band
    )


def test_no_other_ink_inside_the_runway_band():
    svg = _render(**_mock())
    bad = _ink_inside_band(svg)
    assert not bad, f"{len(bad)} ink samples under the runway, e.g. {bad[:3]}"


def test_crossing_lines_meet_the_strip_edge_with_no_gap():
    svg = _render(**_mock())
    band = _band(svg)
    groups = _groups(svg)
    cut_ends = []
    for gid in ("osm-taxiways", "context-roads", "osm-aprons"):
        for poly in groups[gid]:
            cut_ends += [poly[0], poly[-1]]
    ends_near_band = [p for p in cut_ends if _dist_to_polygon_edge(p, band) < 6.0]
    assert len(ends_near_band) >= 6, "expected cuts at the runway"
    for p in ends_near_band:
        d = _dist_to_polygon_edge(p, band)
        assert d <= FLUSH_TOL, f"cut ends {d:.2f} units off the strip edge, want flush (no gap)"


def _dist_to_polygon_edge(pt, poly):
    best = 1e9
    for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1]):
        dx, dy = x1 - x0, y1 - y0
        n = dx * dx + dy * dy
        t = 0.0 if n == 0 else max(0.0, min(1.0, ((pt[0] - x0) * dx + (pt[1] - y0) * dy) / n))
        best = min(best, math.hypot(pt[0] - x0 - t * dx, pt[1] - y0 - t * dy))
    return best


def test_lines_remain_on_both_sides_and_far_lines_are_untouched():
    svg = _render(**_mock())
    groups = _groups(svg)
    band = _band(svg)
    # The crossing taxiway survives as two pieces, one on each side of the strip.
    sides = {"left": 0, "right": 0}
    for poly in groups["osm-taxiways"]:
        for pt in (poly[0], poly[-1]):
            if _dist_to_polygon_edge(pt, band) < 6.0:
                cx = sum(p[0] for p in band) / 4
                sides["left" if pt[0] < cx else "right"] += 1
    assert sides["left"] and sides["right"], sides
    # The far taxiway never touched the band: a single, uncut path.
    far = [p for p in groups["osm-taxiways"] if _dist_to_polygon_edge(p[0], band) > 40 and _dist_to_polygon_edge(p[-1], band) > 40
           and len(p) == 2]
    assert far, "far taxiway should be one uncut path"
    # The road is cut (>= 2 pieces from the one input line) and lies outside the band.
    assert len(groups["context-roads"]) >= 2


def test_runway_ink_is_never_cut():
    svg = _render(**_mock())
    strip = _groups(svg)["runways"]
    # All 25 infill lines intact: single straight segments of identical length.
    assert len(strip) == 25 and all(len(p) == 2 for p in strip)
    lengths = {round(math.hypot(p[1][0] - p[0][0], p[1][1] - p[0][1]), 1) for p in strip}
    assert len(lengths) == 1, lengths
    # Idents drawn once each; marks group present.
    assert svg.count(">13L<") == 1 and svg.count(">31R<") == 1
    assert "runway-marks" in _groups(svg)


def test_priority_applies_to_every_non_runway_layer():
    """Hangars, stands, aprons, roads, taxiways: all yield to the runway."""
    straddle = [_offset(LE, 300, -120), _offset(LE, 300, 120), _offset(LE, 500, 120),
                _offset(LE, 500, -120), _offset(LE, 300, -120)]   # wider than the band
    inside = [_offset(LE, 300, -20), _offset(LE, 300, 20), _offset(LE, 500, 20),
              _offset(LE, 500, -20), _offset(LE, 300, -20)]        # fully under the strip
    svg = _render(taxiways=[[_offset(LE, 350, -200), _offset(LE, 350, 200)]],
                  roads=[[_offset(LE, 250, 5), _offset(LE, 600, 5)]],
                  aprons=[straddle], hangars=[inside])
    assert not _ink_inside_band(svg)
    groups = _groups(svg)
    # The straddling closed outline is reduced to the pieces outside the band...
    assert len(groups["osm-aprons"]) >= 2
    # ...and an outline entirely under the runway disappears (runway wins).
    assert groups["osm-hangars"] == []


def test_no_runway_in_view_means_no_cut():
    """Runway layer off / no runway: nothing is cut (no band exists)."""
    taxi = [_offset(LE, 400, -250), _offset(LE, 400, 250)]
    plain = _render([taxi], runways=False, layers=["taxiway"])
    assert len(_groups(plain)["osm-taxiways"]) == 1
    # Runway layer deselected while the runway exists: its band is not drawn, so no cut either.
    no_rwy = _render([taxi], layers=["taxiway"])
    assert len(_groups(no_rwy)["osm-taxiways"]) == 1
