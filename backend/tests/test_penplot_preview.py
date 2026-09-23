"""P1 result-cache + P2 default-off vector preview behaviour over real HTTP.

P1: repeated converts with the same effective params are content-addressed —
identical svg_url and response body, and raster-only slider churn does not
change the vector result identity. P2: vector converts skip linesort/reloop
by default (the drawn geometry is unchanged; only pen travel differs), which
is visible in the vpype recipe and the ``travel_optimization_off`` warning.
"""

from __future__ import annotations

from backend.tests.helpers import default_params, png_bytes, svg_bytes, upload


def _convert(http_client, image_id: str, params: dict) -> dict:
    resp = http_client.post(
        "/v1/convert", json={"image_id": image_id, "params": params}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _upload_svg(http_client) -> str:
    return upload(http_client, svg_bytes(), "v.svg").json()["image_id"]


def _upload_png(http_client) -> str:
    return upload(http_client, png_bytes(), "p.png").json()["image_id"]


# -- P2: default-off travel stages for vector previews ---------------------

def test_vector_preview_skips_linesort_and_reloop(http_client):
    image_id = _upload_svg(http_client)
    body = _convert(http_client, image_id, default_params("contour"))
    assert "travel_optimization_off" in body["warnings"]
    assert "linesort" not in body["vpype_command"]
    assert "reloop" not in body["vpype_command"]
    assert body["stats"]["strokes"] >= 1


def test_vector_full_quality_re_enables_travel_optimization(http_client):
    image_id = _upload_svg(http_client)
    params = default_params("contour")
    params["full_quality"] = True
    body = _convert(http_client, image_id, params)
    assert "travel_optimization_off" not in body["warnings"]
    assert "linesort" in body["vpype_command"]
    assert "reloop" in body["vpype_command"]
    assert body["stats"]["strokes"] >= 1


def test_raster_input_still_optimizes_travel_by_default(http_client):
    image_id = _upload_png(http_client)
    body = _convert(http_client, image_id, default_params("hatch"))
    assert "travel_optimization_off" not in body["warnings"]
    assert "linesort" in body["vpype_command"]
    assert "reloop" in body["vpype_command"]


# -- P1: content-addressed repeats -----------------------------------------

def test_repeat_vector_convert_is_cached_identical(http_client):
    image_id = _upload_svg(http_client)
    params = default_params("contour")
    first = _convert(http_client, image_id, params)
    second = _convert(http_client, image_id, params)
    assert second["svg_url"] == first["svg_url"]
    assert second == first


def test_vector_result_ignores_raster_only_param_churn(http_client):
    image_id = _upload_svg(http_client)
    base = _convert(http_client, image_id, default_params("contour"))
    # Threshold/method/contrast never reach vector geometry — the effective
    # param hash must ignore them, so the result identity does not fragment.
    tweaked = default_params("contour")
    tweaked["threshold"] = 42
    tweaked["contrast"] = 2.5
    tweaked["method"] = "hatch"
    second = _convert(http_client, image_id, tweaked)
    assert second["svg_url"] == base["svg_url"]
    assert http_client.get(base["svg_url"]).text == http_client.get(second["svg_url"]).text


def test_vector_fast_preview_ignores_linesort_flag(http_client):
    image_id = _upload_svg(http_client)
    params = default_params("contour")
    fast = _convert(http_client, image_id, params)
    sorted_on = dict(params)
    sorted_on["linesort"] = False
    also_fast = _convert(http_client, image_id, sorted_on)
    assert also_fast["svg_url"] == fast["svg_url"]


def test_full_quality_is_a_distinct_result(http_client):
    image_id = _upload_svg(http_client)
    fast = _convert(http_client, image_id, default_params("contour"))
    params = default_params("contour")
    params["full_quality"] = True
    full = _convert(http_client, image_id, params)
    assert full["svg_url"] != fast["svg_url"]
    assert full["stats"]["pen_up_mm"] <= fast["stats"]["pen_up_mm"] + 1e-6