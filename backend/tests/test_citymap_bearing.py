"""Citymap render bearing (picker rotation) — TC-D29 backend half.

The picker can be rotated, and the plotted artwork has to follow it
instead of staying north-up. ``render_svg`` takes the reported
``bearing_deg`` plus the ``viewport_aspect`` (the framed rect cannot be
recovered from an axis-aligned bbox alone once rotated), rotates content
about the bbox centre, and clips it to the framed viewport rect with
Liang-Barsky.

Everything here is hermetic: synthetic geometry in, SVG string out.
Overpass/Nominatim are never touched.
"""

from __future__ import annotations

import math
import re

import pytest

from backend.citymap.render import render_svg
from backend.citymap.router import _render_keys
from backend.citymap.schemas import BBox, RenderRequest

# A ~0.1° box near Budapest's latitude. lon0/lat0 land mid-box, so an
# east-west street (constant lat) is a horizontal line in plane meters.
BBOX = (47.45, 19.00, 47.55, 19.10)
BBOX_OBJ = BBox(south=47.45, west=19.00, north=47.55, east=19.10)
LAYERS = ["roads"]
A4_LANDSCAPE = 210.0 / 297.0  # the picker keeps the paper ratio


def _ew_street(lon0: float = 19.01, lon1: float = 19.09) -> dict[str, list]:
    """A single east-west street: constant latitude, varying longitude."""
    return {"roads": [[(lon0, 47.50), (lon1, 47.50)]]}


def _ew_street_crossing_centre() -> dict[str, list]:
    """An E-W street that runs well past the bbox edges on both sides."""
    return {"roads": [[(18.90, 47.50), (19.20, 47.50)]]}


_PATH_RE = re.compile(r'<path d="([^"]+)"/>')
_COORD_RE = re.compile(r"(-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)")


def _paths(svg: str) -> list[list[tuple[float, float]]]:
    """Every ``<path>`` in the document as a list of (x, y) points."""
    out: list[list[tuple[float, float]]] = []
    for d in _PATH_RE.findall(svg):
        pts = [(float(x), float(y)) for x, y in _COORD_RE.findall(d)]
        if pts:
            out.append(pts)
    return out


def _viewbox(svg: str) -> tuple[float, float]:
    """(width, height) from the root <svg> element."""
    m = re.search(
        r'<svg[^>]*width="([\d.]+)"[^>]*height="([\d.]+)"', svg
    )
    assert m, f"no svg root with width/height in: {svg[:200]}"
    return float(m.group(1)), float(m.group(2))


def _bbox_of(paths: list[list[tuple[float, float]]]) -> tuple[float, float, float, float]:
    xs = [x for p in paths for x, _ in p]
    ys = [y for p in paths for _, y in p]
    assert xs and ys, "no geometry to measure"
    return min(xs), min(ys), max(xs), max(ys)


# --- 90°: an east-west street must come out vertical ----------------------


def test_bearing_90_draws_east_west_street_vertical():
    svg, counts = render_svg(
        _ew_street(), BBOX, LAYERS, bearing_deg=90, viewport_aspect=A4_LANDSCAPE
    )
    assert counts["roads"] == 1
    paths = _paths(svg)
    assert len(paths) == 1
    x0, y0, x1, y1 = _bbox_of(paths)
    # Vertical: tall in y, hairline in x.
    assert (y1 - y0) > (x1 - x0) * 20, (
        f"bearing 90 should draw the E-W street vertical; got "
        f"dx={x1 - x0:.2f} dy={y1 - y0:.2f}"
    )


def test_bearing_0_leaves_east_west_street_horizontal():
    svg, _ = render_svg(_ew_street(), BBOX, LAYERS)
    x0, y0, x1, y1 = _bbox_of(_paths(svg))
    assert (x1 - x0) > (y1 - y0) * 20, (
        f"north-up should stay horizontal; got dx={x1 - x0:.2f} dy={y1 - y0:.2f}"
    )


def test_bearing_270_also_draws_vertical():
    # 270 is the other quarter turn; it must be vertical too (just flipped).
    svg, _ = render_svg(
        _ew_street(), BBOX, LAYERS, bearing_deg=270, viewport_aspect=A4_LANDSCAPE
    )
    x0, y0, x1, y1 = _bbox_of(_paths(svg))
    assert (y1 - y0) > (x1 - x0) * 20


# --- rotation direction: content turns counter-clockwise as bearing grows -


def test_bearing_direction_matches_maplibre_east_up():
    # MapLibre bearing is the compass direction that is "up": at 90° east
    # is up, so ground content turns counter-clockwise on screen as the
    # bearing grows (turning your head right shifts the world left). A
    # clockwise rotation passes every quarter-turn test above (90°/270°
    # are symmetric) while visibly mirroring the picker at small angles,
    # so pin the sign with a 10° tilt of an east-west street (SVG y grows
    # downwards, so "east end up" reads as a smaller y).
    svg, _ = render_svg(
        _ew_street(), BBOX, LAYERS, bearing_deg=10, viewport_aspect=A4_LANDSCAPE
    )
    paths = _paths(svg)
    assert len(paths) == 1
    west_end, east_end = paths[0][0], paths[0][-1]
    assert east_end[1] < west_end[1], (
        f"bearing 10 should tilt the E-W street's east end up; got "
        f"west_y={west_end[1]:.2f} east_y={east_end[1]:.2f}"
    )
    # Mirrored bearing mirrors the tilt.
    svg2, _ = render_svg(
        _ew_street(), BBOX, LAYERS, bearing_deg=350, viewport_aspect=A4_LANDSCAPE
    )
    paths2 = _paths(svg2)
    assert len(paths2) == 1
    west2, east2 = paths2[0][0], paths2[0][-1]
    assert east2[1] > west2[1], (
        f"bearing 350 should tilt the E-W street's east end down; got "
        f"west_y={west2[1]:.2f} east_y={east2[1]:.2f}"
    )


# --- 45°: content is clipped to the framed viewport rect -----------------


def test_bearing_45_clips_to_viewport_rect():
    svg, _ = render_svg(
        _ew_street_crossing_centre(),
        BBOX,
        LAYERS,
        bearing_deg=45,
        viewport_aspect=A4_LANDSCAPE,
    )
    w, h = _viewbox(svg)
    x0, y0, x1, y1 = _bbox_of(_paths(svg))
    # Liang-Barsky keeps geometry inside the framed rect, so nothing may
    # escape the canvas. A half-pixel of slack absorbs the .2f rounding.
    assert x0 >= -0.5 and y0 >= -0.5, f"clipped art escapes left/top: {x0:.2f},{y0:.2f}"
    assert x1 <= w + 0.5 and y1 <= h + 0.5, f"clipped art escapes right/bottom: {x1:.2f},{y1:.2f}"


def test_bearing_45_actually_clips_a_long_street():
    # Same 45° render, but the street is long enough that clipping has to
    # bite: the kept run must be shorter than the input polyline.
    long_street = _ew_street_crossing_centre()
    svg, _ = render_svg(
        long_street, BBOX, LAYERS, bearing_deg=45, viewport_aspect=A4_LANDSCAPE
    )
    paths = _paths(svg)
    kept = max(math.dist(a, b) for p in paths for a, b in zip(p, p[1:]))
    rotated_street_span = math.hypot(0.30 * 111320.0 * math.cos(math.radians(47.5)),
                                     0.30 * 110540.0)
    # The street is clipped well short of its full length.
    assert kept < rotated_street_span * 0.75, (
        f"45° street was not clipped: kept={kept:.1f}m of ~{rotated_street_span:.1f}m"
    )


def test_rotation_can_split_a_street_into_multiple_runs():
    # A diagonal that leaves the framed rect must survive as separate runs
    # rather than a single path shooting across the canvas.
    diag = {
        "roads": [
            [(19.00, 47.45), (19.10, 47.55)],
            [(19.10, 47.45), (19.00, 47.55)],
        ]
    }
    svg, counts = render_svg(
        diag, BBOX, LAYERS, bearing_deg=45, viewport_aspect=A4_LANDSCAPE
    )
    w, h = _viewbox(svg)
    assert counts["roads"] >= 1
    for p in _paths(svg):
        for x, y in p:
            assert -0.5 <= x <= w + 0.5 and -0.5 <= y <= h + 0.5


# --- the framed rect comes from the aspect, not the content --------------


def test_viewport_aspect_drives_the_canvas_ratio():
    landscape, _ = render_svg(
        _ew_street(), BBOX, LAYERS, bearing_deg=45, viewport_aspect=A4_LANDSCAPE
    )
    portrait, _ = render_svg(
        _ew_street(), BBOX, LAYERS, bearing_deg=45, viewport_aspect=1.0 / A4_LANDSCAPE
    )
    lw, lh = _viewbox(landscape)
    pw, ph = _viewbox(portrait)
    # Canvas ratio follows the picker viewport, and the two are reciprocal.
    assert (lw / lh) == pytest.approx(A4_LANDSCAPE, rel=1e-3)
    assert (pw / ph) == pytest.approx(1.0 / A4_LANDSCAPE, rel=1e-3)


def test_canvas_aspect_tracks_aspect_at_several_bearings():
    for bearing in (15, 45, 75, 135, 200):
        svg, _ = render_svg(
            _ew_street(), BBOX, LAYERS, bearing_deg=bearing, viewport_aspect=A4_LANDSCAPE
        )
        w, h = _viewbox(svg)
        assert (w / h) == pytest.approx(A4_LANDSCAPE, rel=1e-3), (
            f"bearing {bearing} lost the viewport aspect: {w}x{h}"
        )


# --- bearing without aspect is a contract error ---------------------------


def test_render_svg_requires_aspect_with_non_zero_bearing():
    with pytest.raises(ValueError, match="viewport_aspect"):
        render_svg(_ew_street(), BBOX, LAYERS, bearing_deg=45)


@pytest.mark.parametrize("bearing", [0.5, 45, 90, 359.5, -45, 360.5, 719.5])
def test_schema_rejects_bearing_without_viewport_aspect(bearing):
    with pytest.raises(ValueError, match="viewport_aspect"):
        RenderRequest(bbox=BBOX_OBJ, layers=LAYERS, bearing_deg=bearing)


@pytest.mark.parametrize("bearing", [0, 360, 720, -360, 0.0])
def test_schema_accepts_north_up_bearings_without_aspect(bearing):
    # 0/±360 are north-up: the aspect is meaningless there and the wire
    # stays clean, so these must validate.
    req = RenderRequest(bbox=BBOX_OBJ, layers=LAYERS, bearing_deg=bearing)
    assert req.viewport_aspect is None


def test_schema_accepts_bearing_with_aspect():
    req = RenderRequest(
        bbox=BBOX_OBJ, layers=LAYERS, bearing_deg=45, viewport_aspect=A4_LANDSCAPE
    )
    assert req.bearing_deg == 45


@pytest.mark.parametrize("aspect", [0.0, -1.0, 10.5, 11.0])
def test_schema_rejects_out_of_range_aspect(aspect):
    with pytest.raises(ValueError):
        RenderRequest(bbox=BBOX_OBJ, layers=LAYERS, bearing_deg=45, viewport_aspect=aspect)


def test_schema_rejects_absurd_bearing():
    with pytest.raises(ValueError):
        RenderRequest(
            bbox=BBOX_OBJ, layers=LAYERS, bearing_deg=1000, viewport_aspect=A4_LANDSCAPE
        )


# --- north-up is untouched (the compat guarantee) ------------------------


def test_north_up_output_is_byte_identical_without_the_new_kwargs():
    geoms = _ew_street()
    baseline_svg, baseline_counts = render_svg(geoms, BBOX, LAYERS)
    # Same request expressed with the rotation fields present but neutral.
    explicit_svg, explicit_counts = render_svg(
        geoms, BBOX, LAYERS, bearing_deg=0, viewport_aspect=None
    )
    assert explicit_svg == baseline_svg
    assert explicit_counts == baseline_counts


def test_bearing_360_is_north_up_and_byte_identical():
    geoms = _ew_street()
    baseline_svg, _ = render_svg(geoms, BBOX, LAYERS)
    for bearing in (360, 720, -360):
        svg, _ = render_svg(
            geoms, BBOX, LAYERS, bearing_deg=bearing, viewport_aspect=A4_LANDSCAPE
        )
        assert svg == baseline_svg, f"bearing {bearing} is north-up and must be identical"


def test_north_up_aspect_ignored():
    # An aspect sent alongside a north-up bearing must not reshape anything
    # (the frontend omits it, but an old client could still send it).
    geoms = _ew_street()
    baseline_svg, _ = render_svg(geoms, BBOX, LAYERS)
    svg, _ = render_svg(geoms, BBOX, LAYERS, bearing_deg=0, viewport_aspect=0.5)
    assert svg == baseline_svg


# --- the cache key only grows while rotation is active -------------------


def _keys(bearing_deg: float = 0.0, aspect: float | None = None) -> tuple[str, str]:
    return _render_keys(
        BBOX, LAYERS,
        RenderRequest(
            bbox=BBOX_OBJ, layers=LAYERS, bearing_deg=bearing_deg, viewport_aspect=aspect
        ),
    )


def test_north_up_cache_keys_are_unchanged_by_the_new_fields():
    plain = _keys()
    explicit_zero = _keys(0.0, None)
    assert plain == explicit_zero


def test_north_up_cache_keys_survive_a_bogus_aspect():
    # North-up + aspect: the key must not move, so a north-up cache entry
    # is never shadowed by a stray aspect from some client.
    assert _keys(0.0, 1.414) == _keys()
    assert _keys(360.0, 1.414) == _keys()


def test_rotated_keys_differ_from_north_up():
    assert _keys(90, A4_LANDSCAPE) != _keys()


def test_rotated_keys_differ_per_bearing():
    assert _keys(45, A4_LANDSCAPE) != _keys(90, A4_LANDSCAPE)


def test_rotated_keys_differ_per_aspect():
    assert _keys(45, 1.0) != _keys(45, A4_LANDSCAPE)


def test_equivalent_bearings_share_a_cache_entry():
    # 45, 405 and -315 are the same rotation; they must not each miss.
    assert _keys(45, A4_LANDSCAPE) == _keys(405, A4_LANDSCAPE) == _keys(-315, A4_LANDSCAPE)


def test_svg_and_counts_keys_stay_distinct():
    svg_key, counts_key = _keys(90, A4_LANDSCAPE)
    assert svg_key != counts_key
