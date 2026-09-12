"""Tone controls: brightness offset + paper-background removal.

Units cover the pixel math; HTTP covers the wire surface (cache keys differ,
warnings surface, out-of-range 422s).
"""

from __future__ import annotations

import numpy as np

from backend.penplot import imaging
from backend.tests.helpers import default_params, png_bytes, upload


def _vignette(w: int = 200, h: int = 150) -> np.ndarray:
    """Gray paper gradient (180→120) with a dark ink bar — a scan-like fixture."""
    xs = np.tile(np.linspace(180, 120, w, dtype=np.float32), (h, 1))
    xs[60:90, 40:160] = 40.0  # ink bar
    return xs.astype(np.uint8)


def test_brightness_zero_is_identity():
    gray = _vignette()
    assert np.array_equal(imaging.adjust_brightness(gray, 0.0), gray)


def test_brightness_shifts_mean_both_directions():
    gray = _vignette()
    up = imaging.adjust_brightness(gray, 50.0)
    down = imaging.adjust_brightness(gray, -50.0)
    assert float(up.mean()) > float(gray.mean()) > float(down.mean())
    assert up.dtype == np.uint8 and down.dtype == np.uint8


def test_brightness_clips_to_8bit():
    white = np.full((8, 8), 250, dtype=np.uint8)
    assert imaging.adjust_brightness(white, 100.0).max() == 255
    black = np.full((8, 8), 5, dtype=np.uint8)
    assert imaging.adjust_brightness(black, -100.0).min() == 0


def test_remove_background_flattens_paper_keeps_ink():
    gray = _vignette()
    clean = imaging.remove_background(gray)
    paper_before = np.concatenate([gray[5, :], gray[-5, :]]).astype(float)
    paper_after = np.concatenate([clean[5, :], clean[-5, :]]).astype(float)
    # Paper gradient (std ~17) flattens toward white...
    assert paper_after.std() < paper_before.std()
    assert paper_after.mean() > 240.0
    # ...while the ink bar survives darker than the paper.
    assert clean[70, 100] < paper_after.mean() - 30.0


def test_remove_background_uniform_blank_stays_white():
    blank = np.full((64, 48), 255, dtype=np.uint8)
    assert imaging.remove_background(blank).min() == 255


def _convert(http_client, image_id: str, params: dict):
    return http_client.post("/v1/convert", json={"image_id": image_id, "params": params})


def test_brightness_changes_output_over_http(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    urls = set()
    for brightness in (0.0, 80.0):
        params = default_params("hatch")
        params["brightness"] = brightness
        urls.add(_convert(http_client, image_id, params).json()["svg_url"])
    assert len(urls) == 2


def test_brightness_out_of_range_422(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = default_params("hatch")
    params["brightness"] = 999.0
    resp = _convert(http_client, image_id, params)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


def test_remove_background_warns_and_changes_output_over_http(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    plain = _convert(http_client, image_id, default_params("hatch")).json()
    params = default_params("hatch")
    params["remove_background"] = True
    cleaned = _convert(http_client, image_id, params).json()
    assert "background_removed" in cleaned["warnings"]
    assert "background_removed" not in plain["warnings"]
    assert cleaned["svg_url"] != plain["svg_url"]
