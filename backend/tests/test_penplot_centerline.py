"""Centerline (skeleton) tracing + Chaikin curve smoothing.

Centerline collapses each ink stroke to its 1-px medial axis before tracing,
so monoline drawings yield single strokes where the contour tracer follows
both edges of the band. Curve smoothing rounds faceted corners post-layout
(the potrace alphamax / vtracer spline stand-in); 0 disables it (identity).
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageDraw

from backend.penplot.methods import METHOD_REGISTRY, MethodContext
from backend.penplot.optimize import count_points, curvesmooth
from backend.tests.helpers import default_params, png_bytes, upload


def _ctx(**overrides) -> MethodContext:
    base = {
        "threshold": 128,
        "blur_radius": 1.0,
        "hatch_pitch_mm": 1.2,
        "contour_simplify": 1.0,
        "centerline_prune_px": 4,
    }
    base.update(overrides)
    return MethodContext(**base)


def _ring_mask(size: int = 200, r_out: int = 60, r_in: int = 50) -> np.ndarray:
    yy, xx = np.mgrid[0:size, 0:size]
    r = np.hypot(xx - size // 2, yy - size // 2)
    return (r > r_in) & (r < r_out)


def _ring_png_bytes(size: int = 200) -> bytes:
    img = Image.new("L", (size, size), 255)
    draw = ImageDraw.Draw(img)
    draw.ellipse(
        [size // 2 - 60, size // 2 - 60, size // 2 + 60, size // 2 + 60],
        outline=0,
        width=10,
    )
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def test_centerline_ring_is_single_loop_not_double_edge():
    """A thick ring: contour finds inner+outer edges, centerline finds one."""
    mask = _ring_mask()
    gray = np.where(mask, 0, 255).astype(np.uint8)
    center = METHOD_REGISTRY["centerline"].generate(mask, gray, _ctx())
    outline = METHOD_REGISTRY["contour"].generate(mask, gray, _ctx())
    assert len(center) == 1, [len(p) for p in center]
    assert center[0][0] == center[0][-1]  # closed loop
    assert len(outline) == 2, [len(p) for p in outline]


def test_centerline_blank_is_empty_not_error():
    gray = np.full((50, 50), 255, dtype=np.uint8)
    out = METHOD_REGISTRY["centerline"].generate(
        np.zeros((50, 50), dtype=bool), gray, _ctx()
    )
    assert out == []


def test_centerline_thin_diagonal_survives_end_to_end():
    mask = np.zeros((50, 50), dtype=bool)
    for i in range(5, 45):
        mask[i, i] = True
    gray = np.where(mask, 0, 255).astype(np.uint8)
    out = METHOD_REGISTRY["centerline"].generate(mask, gray, _ctx())
    assert len(out) == 1
    assert out[0][0] == (5.0, 5.0) and out[0][-1] == (44.0, 44.0)


def test_curvesmooth_zero_is_identity():
    lines = [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]]
    assert curvesmooth(lines, 0) == lines


def test_curvesmooth_rounds_and_keeps_closure_and_endpoints():
    closed = [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]]
    one = curvesmooth(closed, 1)
    assert count_points(one) > count_points(closed)
    assert one[0][0] == one[0][-1]  # closed stays closed
    opened = [[(0.0, 0.0), (5.0, 1.0), (10.0, 0.0)]]
    smoothed = curvesmooth(opened, 2)
    assert smoothed[0][0] == opened[0][0]
    assert smoothed[0][-1] == opened[0][-1]


def _convert(http_client, image_id: str, params: dict):
    return http_client.post(
        "/v1/convert", json={"image_id": image_id, "params": params}
    )


def test_centerline_convert_over_http(http_client):
    image_id = upload(http_client, _ring_png_bytes()).json()["image_id"]
    params = default_params("centerline")
    resp = _convert(http_client, image_id, params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stats"]["strokes"] >= 1
    svg = http_client.get(body["svg_url"])
    assert svg.status_code == 200 and "<path" in svg.text


def test_curve_smooth_off_shares_cache_with_default(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    default = _convert(http_client, image_id, default_params("hatch")).json()
    params = default_params("hatch")
    params["curve_smooth"] = 0
    explicit = _convert(http_client, image_id, params).json()
    assert explicit["svg_url"] == default["svg_url"]


def test_curve_smooth_on_changes_output_and_warns(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = default_params("contour")
    params["curve_smooth"] = 2
    resp = _convert(http_client, image_id, params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "curve_smoothed" in body["warnings"]
    plain = _convert(http_client, image_id, default_params("contour")).json()
    assert body["svg_url"] != plain["svg_url"]


def test_centerline_params_out_of_range_422(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    for bad in ({"curve_smooth": 4}, {"curve_smooth": -1},
                {"centerline_prune_px": 51}, {"centerline_prune_px": -1}):
        params = default_params("centerline")
        params.update(bad)
        resp = _convert(http_client, image_id, params)
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json()["error"]["code"] == "invalid_params"


def test_ui_exposes_centerline_and_smooth_controls(http_client):
    html = http_client.get("/penplot").text
    for marker in (
        'id="m_centerline"',
        'id="centerline_prune_px"',
        'id="curve_smooth"',
        'value="centerline"',
    ):
        assert marker in html, marker
