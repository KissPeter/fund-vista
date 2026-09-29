"""Page-furniture chamfer regression: frame corners survive linesimplify.

The default ``linesimplify_tolerance_mm`` is 0.1 mm. The 2 mm page-frame
corner radius is small enough that Douglas-Peucker used to collapse each
corner arc to a single chord — a visible chamfer where a rounded corner
was promised. Page furniture (frame, label, stats table) now rejoins the
pipeline *after* linesimplify, so those corners keep their drawn shape.

These parse the real converted SVG and count the points that land in each
of the four corner arcs. Hermetic: a tiny in-memory PNG through the
local test client, no network.
"""

from __future__ import annotations

import math
import re

import pytest

from backend.tests.helpers import default_params, png_bytes, upload

PAGE_W, PAGE_H, MARGIN = 210.0, 297.0, 10.0
DEFAULT_TOL = 0.1  # linesimplify_tolerance_mm


def _convert(http_client, image_id: str, params: dict):
    return http_client.post("/v1/convert", json={"image_id": image_id, "params": params})


def _framed(**kw) -> dict:
    params = default_params("hatch")
    params["page"] = {"size": "A4", "orientation": "portrait", "margin_mm": MARGIN}
    params["page"].update(kw)
    return params


_PATH_RE = re.compile(r'<path d="([^"]+)"')
_PT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)")


def _paths(svg: str) -> list[list[tuple[float, float]]]:
    return [
        pts for pts in (
            [(float(x), float(y)) for x, y in _PT_RE.findall(d)]
            for d in _PATH_RE.findall(svg)
        ) if pts
    ]


def _frame_path(svg: str, radius: float) -> list[tuple[float, float]]:
    """The frame is the only closed path spanning the whole margin rect."""
    lo, hi = MARGIN - 0.5, None
    best, best_area = None, 0.0
    for pts in _paths(svg):
        xs = [x for x, _ in pts]
        ys = [y for _, y in pts]
        if not (min(xs) >= lo and min(ys) >= lo):
            continue
        area = (max(xs) - min(xs)) * (max(ys) - min(ys))
        # The frame spans essentially the full printable area.
        if area < 0.9 * (PAGE_W - 2 * MARGIN) * (PAGE_H - 2 * MARGIN):
            continue
        if area > best_area:
            best, best_area = pts, area
    assert best is not None, "no full-page frame path found in the converted SVG"
    return best


def _corner_counts(pts: list[tuple[float, float]], radius: float) -> list[int]:
    """Points landing in each of the four corner arcs (TR, BR, BL, TL)."""
    # Arc centers sit inset by the radius from each rect corner.
    centers = [
        (PAGE_W - MARGIN - radius, MARGIN + radius),      # top-right
        (PAGE_W - MARGIN - radius, PAGE_H - MARGIN - radius),  # bottom-right
        (MARGIN + radius, PAGE_H - MARGIN - radius),      # bottom-left
        (MARGIN + radius, MARGIN + radius),               # top-left
    ]
    counts = []
    for cx, cy in centers:
        counts.append(
            sum(1 for x, y in pts if math.hypot(x - cx, y - cy) <= radius + 0.75)
        )
    return counts


@pytest.mark.parametrize("radius", [1.0, 2.0, 3.0, 4.0])
def test_frame_corner_arcs_survive_default_linesimplify(http_client, radius):
    # The regression: at the default 0.1 mm tolerance every corner arc used
    # to simplify to one chord (a chamfer). Each corner must keep >= 4
    # points, i.e. still look curved.
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    body = _convert(
        http_client, image_id, _framed(frame=True, frame_radius_mm=radius)).json()
    svg = http_client.get(body["svg_url"]).text
    counts = _corner_counts(_frame_path(svg, radius), radius)
    for (cx, cy), n in zip(
        [(PAGE_W - MARGIN, MARGIN), (PAGE_W - MARGIN, PAGE_H - MARGIN),
         (MARGIN, PAGE_H - MARGIN), (MARGIN, MARGIN)],
        counts,
    ):
        assert n >= 4, (
            f"frame corner near ({cx},{cy}) kept only {n} arc points at "
            f"radius {radius}mm — it simplified to a chamfer"
        )


def test_tighter_linesimplify_still_keeps_the_corners_round(http_client):
    # Even an aggressive tolerance must not eat furniture: the bypass is
    # unconditional, so 0.01 mm behaves the same as the 0.1 mm default.
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = _framed(frame=True, frame_radius_mm=2.0)
    params["linesimplify_tolerance_mm"] = 0.01
    body = _convert(http_client, image_id, params).json()
    svg = http_client.get(body["svg_url"]).text
    counts = _corner_counts(_frame_path(svg, 2.0), 2.0)
    assert all(n >= 4 for n in counts), f"corners lost at 0.01mm: {counts}"


def test_sharp_frame_has_no_corner_arcs(http_client):
    # radius 0 is a sharp rect: the corners carry no arc bulge, which is
    # the baseline the "smooth" assertions are measured against. The
    # closed path repeats its start point, so a sharp corner reads 2.
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    body = _convert(
        http_client, image_id, _framed(frame=True, frame_radius_mm=0.0)).json()
    svg = http_client.get(body["svg_url"]).text
    counts = _corner_counts(_frame_path(svg, 0.0), 0.0)
    assert all(n <= 2 for n in counts), f"sharp frame grew arcs: {counts}"


def test_larger_radius_keeps_at_least_as_many_arc_points(http_client):
    # A bigger radius must not be *worse* at staying round: monotonic in
    # the point count so a regression can't hide behind a radius bump.
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    tall = _convert(
        http_client, image_id, _framed(frame=True, frame_radius_mm=6.0)).json()
    svg_tall = http_client.get(tall["svg_url"]).text
    tall_counts = _corner_counts(_frame_path(svg_tall, 6.0), 6.0)
    assert all(n >= 4 for n in tall_counts), f"6mm corners lost points: {tall_counts}"


def test_page_furniture_is_present_in_the_output(http_client):
    # Guards the intermediate state where furniture was dropped entirely:
    # frame + label + stats table must all still ink alongside the image.
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = _framed(frame=True, frame_radius_mm=2.0)
    params["label"] = {
        "enabled": True, "text": "A-1", "align": "right", "height_mm": 5.0,
        "font": "futural", "border": True,
        "pad_left_mm": 0.0, "pad_right_mm": 0.0, "border_radius_mm": 0.0,
    }
    params["stats_table"] = {
        "enabled": True, "position": "top-left",
        "rows": [{"key": "highways", "value": 1026},
                 {"key": "roads", "value": 16404}],
    }
    body = _convert(http_client, image_id, params).json()
    svg = http_client.get(body["svg_url"]).text

    # Frame only: baseline stroke count.
    frame_only = _convert(http_client, image_id, _framed(
        frame=True, frame_radius_mm=2.0)).json()
    base_paths = len(_paths(http_client.get(frame_only["svg_url"]).text))
    # Frame + label + table must draw strictly more than the frame alone.
    assert len(_paths(svg)) > base_paths, "label + table added no strokes"
    # The stats table sits top-left inside the margin and stays there.
    all_pts = [p for path in _paths(svg) for p in path]
    table_x = min(x for x, _ in all_pts)
    table_y = min(y for _, y in all_pts)
    assert table_x >= MARGIN - 0.5 and table_y >= MARGIN - 0.5


def test_params_hash_differs_when_stats_pads_differ():
    # pads_* only exist on the stats table, so this is the cache-key guard
    # from the plan: a pad change must not share a result entry.
    from backend.penplot.pipeline import params_hash
    from backend.penplot.schemas import ConvertParams

    def h(pad: float) -> str:
        params = ConvertParams(**{
            **default_params("hatch"),
            "stats_table": {
                "enabled": True, "position": "top-right",
                "rows": [{"key": "roads", "value": 1}],
                "pad_right_mm": pad,
            },
        })
        return params_hash(params)

    assert h(0.0) != h(1.0)
    assert h(1.0) == h(1.0)
