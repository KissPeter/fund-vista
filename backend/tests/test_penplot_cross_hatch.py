"""The cross-hatch opt-out (``params.cross_hatch``, pen-pixel-shop #1095).

The hatch generator always ran a second +90° pass over the darkest areas.
The new flag turns that pass off, so the hatching runs in the
``hatch_angle_deg`` direction only.

Two levels:
- generator unit test — the emitted lines' directions (main angle vs +90°);
- live-HTTP test — the flag reaches ``/v1/convert``, changes the drawing,
  and the default (true) keeps the pre-flag cache entry byte-for-byte.
"""

from __future__ import annotations

import io
import math

import numpy as np
from PIL import Image

from backend.penplot.hatch import HatchMethod
from backend.penplot.methods import MethodContext
from backend.tests.helpers import default_params, upload


def _mask_and_gray(w: int = 200, h: int = 200):
    """A dark disc on white: enough dark area for the cross pass to fire."""
    gray = np.full((h, w), 255, dtype=np.uint8)
    yy, xx = np.mgrid[0:h, 0:w]
    disc = ((xx - w / 2) ** 2 + (yy - h / 2) ** 2) < (min(w, h) / 3) ** 2
    gray[disc] = 0
    return gray < 128, gray


def _line_angles(lines) -> list[float]:
    """Direction of every emitted polyline, folded to [0, 180)."""
    out: list[float] = []
    for line in lines:
        (x0, y0), (x1, y1) = line[0], line[1]
        out.append(math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180.0)
    return out


def _same_angle(a: float, b: float, tol: float = 1.0) -> bool:
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d) <= tol


def _png_bytes(width: int = 200, height: int = 200) -> bytes:
    gray = np.full((height, width), 255, dtype=np.uint8)
    yy, xx = np.mgrid[0:height, 0:width]
    disc = ((xx - width / 2) ** 2 + (yy - height / 2) ** 2) < (min(width, height) / 3) ** 2
    gray[disc] = 0
    img = Image.fromarray(gray, mode="L").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _ctx(**kwargs) -> MethodContext:
    base = dict(threshold=128, blur_radius=1.0, hatch_pitch_mm=1.2, contour_simplify=2.0)
    base.update(kwargs)
    return MethodContext(**base)


def test_generator_cross_hatch_off_has_no_cross_segments():
    mask, gray = _mask_and_gray()
    lines = HatchMethod().generate(mask, gray, _ctx(cross_hatch=False))
    assert lines, "the main hatch pass must still emit lines"
    angles = _line_angles(lines)
    assert all(_same_angle(a, 45.0) for a in angles), angles[:5]
    assert not any(_same_angle(a, 135.0) for a in angles)


def test_generator_cross_hatch_on_keeps_the_cross_pass():
    mask, gray = _mask_and_gray()
    lines = HatchMethod().generate(mask, gray, _ctx(cross_hatch=True))
    angles = _line_angles(lines)
    assert any(_same_angle(a, 45.0) for a in angles)
    assert any(_same_angle(a, 135.0) for a in angles), "the +90° pass must fire"


def test_generator_cross_hatch_default_is_on():
    """MethodContext defaults to the pre-flag behaviour (cross pass on)."""
    mask, gray = _mask_and_gray()
    default_lines = HatchMethod().generate(mask, gray, _ctx())
    on_lines = HatchMethod().generate(mask, gray, _ctx(cross_hatch=True))
    assert default_lines == on_lines


def _convert(http_client, image_id: str, params: dict):
    return http_client.post("/v1/convert", json={"image_id": image_id, "params": params})


def test_cross_hatch_false_draws_less_than_the_default(http_client):
    image_id = upload(http_client, _png_bytes()).json()["image_id"]

    default = _convert(http_client, image_id, default_params("hatch"))
    assert default.status_code == 200, default.text
    params = default_params("hatch")
    params["cross_hatch"] = False
    off = _convert(http_client, image_id, params)
    assert off.status_code == 200, off.text

    assert off.json()["stats"]["strokes"] < default.json()["stats"]["strokes"]
    # Different params -> different (deterministic) cache entry.
    assert off.json()["svg_url"] != default.json()["svg_url"]


def test_cross_hatch_true_shares_the_pre_flag_cache_entry(http_client):
    """true is the pre-flag behaviour: the key drops it, so the explicit
    true and the omitted default address the same stored result."""
    image_id = upload(http_client, _png_bytes()).json()["image_id"]

    omitted = _convert(http_client, image_id, default_params("hatch")).json()
    params = default_params("hatch")
    params["cross_hatch"] = True
    explicit = _convert(http_client, image_id, params).json()
    assert explicit["svg_url"] == omitted["svg_url"]


def test_cross_hatch_out_of_range_422(http_client):
    image_id = upload(http_client, _png_bytes()).json()["image_id"]
    # Pydantic's lax bool coercion ("no" -> False, 1 -> True) is the same as
    # every other bool flag in the schema; only uncoercible values 422.
    for bad in (None, [], "maybe", {"on": True}):
        params = default_params("hatch")
        params["cross_hatch"] = bad
        resp = _convert(http_client, image_id, params)
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json()["error"]["code"] == "invalid_params"
