"""Whole-page frame + cleanup-domain isolation, over real HTTP."""

from __future__ import annotations

import io
import re

from PIL import Image as PILImage

from backend.penplot import labels
from backend.tests.helpers import default_params, png_bytes, upload

PAGE_W, PAGE_H, MARGIN = 210.0, 297.0, 10.0


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


def test_page_frame_and_label_band_are_independent(http_client):
    # The title-block band and the whole-page frame are two separate page
    # furniture elements: with both on the sheet carries both (the frame is
    # no longer swallowed by the label border).
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
    # The page frame adds its own stroke on top of the band.
    assert both["stats"]["strokes"] == label_only["stats"]["strokes"] + 1
    svg_a = http_client.get(label_only["svg_url"]).text
    svg_b = http_client.get(both["svg_url"]).text
    assert svg_a != svg_b
    # The frame is the closed rect spanning the whole margin area; the band
    # is the small closed rect in the bottom strip.
    boxes = _path_boxes(svg_b)
    assert any(
        abs(x0 - MARGIN) < 0.5 and abs(x1 - (210.0 - MARGIN)) < 0.5
        and abs(y0 - MARGIN) < 0.5 and abs(y1 - (297.0 - MARGIN)) < 0.5
        for x0, y0, x1, y1 in boxes
    ), "the page frame is missing next to the title block"
    assert any(
        abs(y1 - (297.0 - MARGIN)) < 0.5 and (y1 - y0) < 30.0
        and (x1 - x0) > 100.0
        for x0, y0, x1, y1 in boxes
    ), "the title-block band is missing"


def _path_boxes(svg: str) -> list[tuple[float, float, float, float]]:
    """Bounding boxes (x0, y0, x1, y1) of every path in an SVG."""
    out: list[tuple[float, float, float, float]] = []
    for d in re.findall(r'<path d="([^"]+)"', svg):
        pts = [
            (float(x), float(y))
            for x, y in re.findall(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", d)
        ]
        if not pts:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        out.append((min(xs), min(ys), max(xs), max(ys)))
    return out


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
    # band/frame; separate domains keep every domain on its own side.
    img = PILImage.new("RGB", (400, 60), "black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    image_id = upload(http_client, buf.getvalue()).json()["image_id"]
    label = {"enabled": True, "text": "SCALE 1:50", "align": "right",
             "height_mm": 5.0, "font": "futural", "border": True,
             "pad_left_mm": 0.0, "pad_right_mm": 0.0, "border_radius_mm": 0.0}
    params = default_params("hatch")
    params["label"] = label
    params["page"] = {"size": "A4", "orientation": "portrait", "margin_mm": 10.0,
                      "frame": True, "frame_radius_mm": 0.0}
    params["linemerge_tolerance_mm"] = 5.0
    body = _convert(http_client, image_id, params).json()
    svg = http_client.get(body["svg_url"]).text
    band_top = 297.0 - 10.0 - labels.label_reserve_mm(
        "SCALE 1:50", height_mm=5.0, font="futural", border=True)
    paths = re.findall(r"<path d=\"([^\"]+)\"/>", svg)
    assert paths
    spanning = 0
    for d in paths:
        ys = [float(v[1]) for v in
              re.findall(r"[ML]\s+(-?[\d.]+)\s+(-?[\d.]+)", d)]
        if min(ys) < band_top - 0.5 and max(ys) > band_top + 0.5:
            spanning += 1
    assert spanning == 1  # the page frame only
