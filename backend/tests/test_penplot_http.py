"""Real-HTTP tests for the pen-plot v1 API (spec §2).

Every test here goes through a live uvicorn socket via the ``http_client``
fixture — no TestClient, no in-process ASGI shortcuts. If it passes here,
the same bytes work against production.
"""

from __future__ import annotations

import pytest

from backend.tests.helpers import (
    default_params,
    empty_svg_bytes,
    png_bytes,
    png_with_svg_metadata_bytes,
    sha256_hex,
    svg_bytes,
    tiny_png_bytes,
    upload,
)

# ---------------------------------------------------------------- upload ---


def test_upload_png_returns_sha256_image_id(http_client):
    data = png_bytes()
    resp = upload(http_client, data)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["image_id"] == sha256_hex(data)  # server recomputes, never trusts client
    assert body["format"] == "png"
    assert body["width"] == 320 and body["height"] == 240
    assert body["is_vector"] is False
    assert body["expires_at"]  # TTL surfaced per spec §2.1


def test_upload_is_idempotent_same_bytes_same_id(http_client):
    data = png_bytes()
    first = upload(http_client, data).json()["image_id"]
    second = upload(http_client, data).json()["image_id"]
    assert first == second


def test_upload_png_with_svg_metadata_accepted_as_raster(http_client):
    """Regression: a valid PNG whose raster metadata mentions `<svg` (XMP in
    design-tool exports) must upload as a raster. The old naive sniff
    classified it as SVG and 400'd with "Uploaded SVG is not well-formed
    XML.", breaking the "we accept all image types" promise."""
    data = png_with_svg_metadata_bytes()
    assert b"<svg" in data.lstrip()[:2048].lower()
    resp = upload(http_client, data, "photo.png")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["format"] == "png"
    assert body["is_vector"] is False
    assert body["image_id"] == sha256_hex(data)


def test_get_image_roundtrip(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    resp = http_client.get(f"/v1/images/{image_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["image_id"] == image_id


def test_get_unknown_image_404_envelope(http_client):
    resp = http_client.get("/v1/images/" + "0" * 64)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "image_not_found"


def test_upload_oversize_413(http_client):
    big = b"x" * (26 * 1024 * 1024)  # size gate runs before format sniffing
    resp = upload(http_client, big, "huge.png")
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


def test_upload_unsupported_format_422(http_client):
    resp = upload(http_client, b"hello, i am not an image", "note.txt")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unsupported_media_type"


def test_upload_empty_svg_400(http_client):
    resp = upload(http_client, empty_svg_bytes(), "empty.svg")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_image"


def test_upload_tiny_image_warns_low_resolution(http_client):
    body = upload(http_client, tiny_png_bytes(), "tiny.png").json()
    assert "low_resolution_for_a4" in body["warnings"]


def test_upload_svg_vector_flag(http_client):
    body = upload(http_client, svg_bytes(), "shapes.svg").json()
    assert body["is_vector"] is True
    assert body["width"] == 210 and body["height"] == 297
    assert body["warnings"] == []  # DPI check is raster-only (spec §4.1)

# ---------------------------------------------------------------- convert ---


def _convert(http_client, image_id: str, params: dict):
    return http_client.post("/v1/convert", json={"image_id": image_id, "params": params})


def _assert_convert_shape(body: dict, image_id: str):
    assert body["image_id"] == image_id
    assert body["svg_url"].startswith("http")
    assert body["svg_url"].endswith("_optimized.svg")
    assert "linemerge" in body["vpype_command"]
    assert "linesimplify" in body["vpype_command"]
    assert "layout" in body["vpype_command"]
    stats = body["stats"]
    assert stats["strokes"] >= 1
    assert stats["pen_down_mm"] > 0
    assert stats["estimated_time_s"] > 0
    assert stats["points"]["after"] <= stats["points"]["before"]
    assert stats["segments"]["after"] <= stats["segments"]["before"]


@pytest.mark.parametrize("method", ["contour", "hatch", "flow"])
def test_convert_all_methods_over_real_http(http_client, method):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    resp = _convert(http_client, image_id, default_params(method))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_convert_shape(body, image_id)

    # The svg_url must actually serve a plottable SVG document.
    svg = http_client.get(body["svg_url"])
    assert svg.status_code == 200
    assert "image/svg+xml" in svg.headers["content-type"]
    assert "<svg" in svg.text and "<path" in svg.text


def test_param_change_needs_no_reupload(http_client):
    """Spec §1 core flow: one upload, N converts with different sliders."""
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    first = _convert(http_client, image_id, default_params("hatch")).json()
    tweaked = default_params("hatch")
    tweaked["hatch_pitch_mm"] = 2.5
    second = _convert(http_client, image_id, tweaked).json()
    assert first["svg_url"] != second["svg_url"]  # different params -> different file
    assert http_client.get(first["svg_url"]).status_code == 200
    assert http_client.get(second["svg_url"]).status_code == 200


def test_convert_is_deterministic_and_cached(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = default_params("contour")
    a = _convert(http_client, image_id, params)
    b = _convert(http_client, image_id, params)
    assert a.json()["svg_url"] == b.json()["svg_url"]
    assert http_client.get(a.json()["svg_url"]).text == http_client.get(b.json()["svg_url"]).text


def test_convert_unknown_image_404(http_client):
    resp = _convert(http_client, "f" * 64, default_params())
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "image_not_found"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(method="crayon"),          # unknown method
        lambda p: p.update(threshold=999),            # out of range
        lambda p: p.update(hatch_pitch_mm=-1.0),      # negative pitch
        lambda p: p.update(contour_simplify=-5.0),
        lambda p: p["page"].update(size="A9"),        # unknown page
        lambda p: p["pen"].update(draw_speed_mm_s=0),  # zero speed
        lambda p: p.update(nonexistent_slider=1),     # typo'd slider (forbid-extra)
    ],
    ids=["method", "threshold", "pitch", "simplify", "page", "speed", "typo"],
)
def test_convert_invalid_params_422_envelope(http_client, mutate):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = default_params()
    mutate(params)
    resp = _convert(http_client, image_id, params)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "invalid_params"


def test_convert_malformed_image_id_422(http_client):
    resp = http_client.post("/v1/convert", json={"image_id": "not-a-hash", "params": {}})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


def test_convert_carries_low_resolution_warning(http_client):
    image_id = upload(http_client, tiny_png_bytes(), "tiny.png").json()["image_id"]
    body = _convert(http_client, image_id, default_params("contour")).json()
    assert "low_resolution_for_a4" in body["warnings"]


def test_convert_svg_vector_input(http_client):
    image_id = upload(http_client, svg_bytes(), "shapes.svg").json()["image_id"]
    resp = _convert(http_client, image_id, default_params("contour"))
    assert resp.status_code == 200, resp.text
    svg = http_client.get(resp.json()["svg_url"])
    assert svg.status_code == 200 and "<path" in svg.text


def test_result_unknown_file_404(http_client):
    resp = http_client.get("/v1/results/does_not_exist_optimized.svg")
    assert resp.status_code == 404


def test_result_path_traversal_blocked(http_client):
    resp = http_client.get("/v1/results/..%2Fmain.py")
    assert resp.status_code in (404, 422)
