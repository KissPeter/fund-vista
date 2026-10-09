"""Vector inputs run the method pipeline (no longer raster-only).

Vectors rasterize first (supersampled, antialiased) and enter the same
tone+method stages as rasters, so every Style-rail slider shapes vector
geometry. All HTTP cases go through a live uvicorn socket via the
``http_client`` fixture — no TestClient, no in-process ASGI shortcuts.
"""

from __future__ import annotations

from backend.penplot import imaging
from backend.penplot.raster import rasterize_polylines, threshold_mask
from backend.tests.helpers import default_params, svg_bytes, upload


def _convert(http_client, image_id: str, params: dict) -> dict:
    resp = http_client.post(
        "/v1/convert", json={"image_id": image_id, "params": params}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _upload_svg(http_client) -> str:
    return upload(http_client, svg_bytes(), "v.svg").json()["image_id"]


def _params(method: str, **overrides) -> dict:
    params = default_params(method)
    params.update(overrides)
    return params


# ------------------------------------------------------- rasterize units ---


def test_rasterize_closed_rings_fill_open_strokes_hairline():
    closed = [[(10.0, 10.0), (100.0, 10.0), (100.0, 50.0), (10.0, 50.0), (10.0, 10.0)]]
    gray = rasterize_polylines(closed, 210, 100, 3000)
    assert gray.shape == (100, 210)
    assert gray.dtype.name == "uint8"
    # Filled rect body: deep interior is pure ink.
    assert gray[30, 55] == 0
    assert (gray < 128).sum() > 1000

    hairline = [[(0.0, 0.0), (200.0, 0.0)]]
    gray = rasterize_polylines(hairline, 210, 100, 3000)
    assert (gray < 128).sum() > 0
    # Solid 1-px core at working res: the default threshold keeps it whole.
    assert (threshold_mask(gray, 128)).sum() == (gray < 128).sum()


def test_rasterize_caps_working_size():
    polys = [[(0.0, 0.0), (4000.0, 0.0), (4000.0, 4000.0), (0.0, 4000.0), (0.0, 0.0)]]
    gray = rasterize_polylines(polys, 4000, 4000, 3000)
    assert gray.shape == (3000, 3000)
    assert gray[1500, 1500] == 0


def test_rasterize_skips_degenerate():
    gray = rasterize_polylines([[], [(1.0, 1.0)]], 210, 100, 3000)
    assert gray.shape == (100, 210)
    assert (gray < 128).sum() == 0


# ------------------------------------------------------------------ HTTP ---


def test_vector_each_method_renders(http_client):
    image_id = _upload_svg(http_client)
    for method in ("contour", "centerline", "hatch", "flow"):
        body = _convert(http_client, image_id, _params(method))
        assert body["stats"]["strokes"] >= 1, method
        assert body["svg_url"].endswith("_optimized.svg")


def test_vector_hatch_differs_from_contour(http_client):
    image_id = _upload_svg(http_client)
    contour = _convert(http_client, image_id, _params("contour"))
    hatch = _convert(http_client, image_id, _params("hatch"))
    assert hatch["svg_url"] != contour["svg_url"]
    first = http_client.get(contour["svg_url"]).text
    second = http_client.get(hatch["svg_url"]).text
    assert first != second
    # Hatching the solid rect+disc shades them: strictly more strokes than
    # tracing their outlines.
    assert hatch["stats"]["strokes"] > contour["stats"]["strokes"]


def test_vector_threshold_sweep_changes_geometry(http_client):
    # Threshold needs raster ink: it shapes centerline/hatch/flow output.
    # (Pure-contour vectors trace directly — there are no pixels to
    # threshold, so the tone sliders stay out of that path by design.)
    image_id = _upload_svg(http_client)
    low = _convert(http_client, image_id, _params("hatch", threshold=10))
    high = _convert(http_client, image_id, _params("hatch", threshold=250))
    assert low["svg_url"] != high["svg_url"]
    assert http_client.get(low["svg_url"]).text != http_client.get(high["svg_url"]).text


def test_vector_contour_ignores_tone_churn(http_client):
    # The complement: pure-contour vectors trace the parsed polylines
    # directly, so tone-slider churn must NOT fragment their identity.
    image_id = _upload_svg(http_client)
    base = _convert(http_client, image_id, _params("contour"))
    tweaked = _params("contour", threshold=42, contrast=2.5)
    second = _convert(http_client, image_id, tweaked)
    assert second["svg_url"] == base["svg_url"]
    assert http_client.get(base["svg_url"]).text == http_client.get(second["svg_url"]).text


def test_vector_hatch_pitch_changes_geometry(http_client):
    image_id = _upload_svg(http_client)
    wide = _convert(http_client, image_id, _params("hatch", hatch_pitch_mm=0.9))
    narrow = _convert(http_client, image_id, _params("hatch", hatch_pitch_mm=2.5))
    assert wide["svg_url"] != narrow["svg_url"]
    assert http_client.get(wide["svg_url"]).text != http_client.get(narrow["svg_url"]).text


def test_vector_repeat_method_convert_is_cached_identical(http_client):
    image_id = _upload_svg(http_client)
    params = _params("hatch", threshold=100)
    first = _convert(http_client, image_id, params)
    second = _convert(http_client, image_id, params)
    assert second["svg_url"] == first["svg_url"]
    assert second == first
