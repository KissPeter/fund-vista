"""Whole-page frame + cleanup-domain isolation, over real HTTP."""

from __future__ import annotations

import io
import re

from PIL import Image as PILImage

from backend.penplot import labels
from backend.tests.helpers import default_params, png_bytes, upload


def _convert(http_client, image_id: str, params: dict):
    return http_client.post("/v1/convert", json={"image_id": image_id, "params": params})


def _with_page(base: dict, **kw) -> dict:
    base["page"] = {"size": "A4", "orientation": "portrait", "margin_mm": 10}
    base["page"].update(kw)
    return base


def test_page_frame_adds_one_stroke_over_http(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    plain = _convert(http_client, image_id, default_params("hatch")).json()
    params = default_params("hatch")
    _with_page(params, frame=True, frame_radius_mm=2.0)
    framed = _convert(http_client, image_id, params).json()
    assert framed["svg_url"] != plain["svg_url"]
    assert framed["stats"]["strokes"] == plain["stats"]["strokes"] + 1


def test_page_frame_skipped_when_label_border_draws_it(http_client):
    # Same margin rect would double-ink: label border wins, page frame yields.
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    label = {"enabled": True, "text": "A-1", "align": "right",
             "height_mm": 5.0, "font": "futural", "border": True,
             "pad_left_mm": 0.0, "pad_right_mm": 0.0, "border_radius_mm": 0.0}
    params = default_params("hatch")
    params["label"] = label
    label_only = _convert(http_client, image_id, params).json()
    params2 = default_params("hatch")
    params2["label"] = dict(label)
    _with_page(params2, frame=True, frame_radius_mm=5.0)
    both = _convert(http_client, image_id, params2).json()
    assert both["stats"]["strokes"] == label_only["stats"]["strokes"]
    svg_a = http_client.get(label_only["svg_url"]).text
    svg_b = http_client.get(both["svg_url"]).text
    assert svg_a == svg_b  # page frame yields: byte-identical, no double ink


def test_page_frame_with_borderless_label(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    label = {"enabled": True, "text": "A-1", "align": "right",
             "height_mm": 5.0, "font": "futural", "border": False,
             "pad_left_mm": 0.0, "pad_right_mm": 0.0, "border_radius_mm": 0.0}
    params = default_params("hatch")
    params["label"] = label
    text_only = _convert(http_client, image_id, params).json()
    params2 = default_params("hatch")
    params2["label"] = dict(label)
    _with_page(params2, frame=True, frame_radius_mm=0.0)
    framed = _convert(http_client, image_id, params2).json()
    assert framed["stats"]["strokes"] == text_only["stats"]["strokes"] + 1


def test_page_frame_invalid_radius_422(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = default_params("hatch")
    _with_page(params, frame=True, frame_radius_mm=99.0)
    resp = _convert(http_client, image_id, params)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


def test_max_linemerge_cannot_fuse_across_strip_over_http(http_client):
    # Solid-ink panorama + label at the max 5 mm merge tolerance: with a
    # joint linemerge the 1 mm strip gap would fuse image strokes into the
    # divider/frame; separate domains keep every domain on its own side.
    img = PILImage.new("RGB", (400, 60), "black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    image_id = upload(http_client, buf.getvalue()).json()["image_id"]
    label = {"enabled": True, "text": "SCALE 1:50", "align": "right",
             "height_mm": 5.0, "font": "futural", "border": True,
             "pad_left_mm": 0.0, "pad_right_mm": 0.0, "border_radius_mm": 0.0}
    params = default_params("hatch")
    params["label"] = label
    params["linemerge_tolerance_mm"] = 5.0
    body = _convert(http_client, image_id, params).json()
    svg = http_client.get(body["svg_url"]).text
    divider_y = 297.0 - 10.0 - labels.label_reserve_mm(
        "SCALE 1:50", height_mm=5.0, font="futural", border=True)
    paths = re.findall(r"<path d=\"([^\"]+)\"/>", svg)
    assert paths
    spanning = 0
    for d in paths:
        ys = [float(v[1]) for v in
              re.findall(r"[ML]\s+(-?[\d.]+)\s+(-?[\d.]+)", d)]
        if min(ys) < divider_y - 0.5 and max(ys) > divider_y + 0.5:
            spanning += 1
    assert spanning == 1  # the frame only
