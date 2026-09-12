"""HTTP-level tests for `strip_hatch_px`: crosshatch removal via morphological
opening on the ink mask, applied after thresholding and before line
generation (contour/hatch/flow). See imaging.strip_hatch for the mechanism.
"""

from __future__ import annotations

import io

import cv2
import numpy as np
from PIL import Image

from backend.tests.helpers import default_params, upload


def _hatched_png_bytes(width: int = 160, height: int = 160) -> bytes:
    """Well-separated vertical hatch lines plus one thick ring standing in
    for real linework (e.g. a bold outline).

    Two deliberate choices, both verified empirically against the default
    pipeline (blur sigma 1.0, threshold 128, 5 px opening kernel):
    - lines are 2 px: 1 px lines vanish in the blur (~139, above threshold)
      before stripping ever runs, so the fixture would exercise nothing;
    - lines are axis-aligned, widely spaced, and kept clear of the ring by a
      white moat: diagonal/joining geometry fragments under opening (raising
      the count) and touching geometry merges into one component (a full
      crosshatch touching the ring traces as a single contour either way).
    """
    gray = np.full((height, width), 255, dtype=np.uint8)
    for x in range(8, width, 16):
        cv2.line(gray, (x, 0), (x, height - 1), color=0, thickness=2)
    cv2.rectangle(gray, (28, 28), (132, 132), color=255, thickness=-1)
    cv2.rectangle(gray, (40, 40), (120, 120), color=0, thickness=10)
    img = Image.fromarray(gray, mode="L").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _convert(http_client, image_id: str, params: dict):
    return http_client.post("/v1/convert", json={"image_id": image_id, "params": params})


def test_strip_hatch_reduces_strokes_and_warns(http_client):
    image_id = upload(http_client, _hatched_png_bytes()).json()["image_id"]

    plain = _convert(http_client, image_id, default_params("contour")).json()
    params = default_params("contour")
    params["strip_hatch_px"] = 5
    stripped_resp = _convert(http_client, image_id, params)
    assert stripped_resp.status_code == 200, stripped_resp.text
    stripped = stripped_resp.json()

    assert stripped["stats"]["strokes"] < plain["stats"]["strokes"]
    assert "hatch_stripped" in stripped["warnings"]
    assert "hatch_stripped" not in plain["warnings"]
    # Different params -> different (deterministic) cache entry.
    assert stripped["svg_url"] != plain["svg_url"]


def test_strip_hatch_zero_matches_omitted_default(http_client):
    image_id = upload(http_client, _hatched_png_bytes()).json()["image_id"]
    omitted = _convert(http_client, image_id, default_params("contour")).json()
    params = default_params("contour")
    params["strip_hatch_px"] = 0
    explicit_zero = _convert(http_client, image_id, params).json()
    assert omitted["svg_url"] == explicit_zero["svg_url"]
    assert "hatch_stripped" not in explicit_zero["warnings"]


def test_strip_hatch_deterministic_cache_hit(http_client):
    image_id = upload(http_client, _hatched_png_bytes()).json()["image_id"]
    params = default_params("contour")
    params["strip_hatch_px"] = 5
    first = _convert(http_client, image_id, params).json()
    again = _convert(http_client, image_id, params).json()
    assert first["svg_url"] == again["svg_url"]


def test_strip_hatch_out_of_range_422(http_client):
    image_id = upload(http_client, _hatched_png_bytes()).json()["image_id"]
    for bad in (-1, 51):
        params = default_params("contour")
        params["strip_hatch_px"] = bad
        resp = _convert(http_client, image_id, params)
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json()["error"]["code"] == "invalid_params"
