"""Display-layer tests: independent pen color + stretched background.

line_color (black/white/red/blue) controls only the SVG stroke;
background (none/dark-texture/light-texture) embeds only a fill-only
layer stretched over the full page. Neither may affect geometry/stats,
and any combination must be accepted.
"""

from __future__ import annotations

import numpy as np

from backend.penplot.backgrounds import (
    BACKGROUNDS,
    LINE_COLORS,
    get_background_data_uri,
    trim_white_border,
)
from backend.tests.helpers import default_params, png_bytes, upload

STROKE = {
    "black": "#000000",
    "white": "#FFFFFF",
    "red": "#FF0000",
    "blue": "#0000FF",
}


def _convert(http_client, image_id: str, **overrides) -> dict:
    params = default_params("hatch")
    params.update(overrides)
    resp = http_client.post("/v1/convert", json={"image_id": image_id, "params": params})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _svg(http_client, body: dict) -> str:
    resp = http_client.get(body["svg_url"])
    assert resp.status_code == 200
    return resp.text


def test_trim_white_border_crops_frame():
    gray = np.full((20, 20), 255, dtype=np.uint8)
    gray[5:15, 4:16] = 10
    out = trim_white_border(gray)
    assert out.shape == (10, 12)


def test_trim_white_border_blank_is_identity():
    gray = np.full((8, 8), 255, dtype=np.uint8)
    assert trim_white_border(gray).shape == (8, 8)


def test_background_registry_and_data_uris():
    assert set(BACKGROUNDS) == {"none", "dark-texture", "light-texture"}
    assert get_background_data_uri("none") is None
    for bid in ("dark-texture", "light-texture"):
        uri = get_background_data_uri(bid)
        assert uri is not None and uri.startswith("data:image/jpeg;base64,")
        assert len(uri) > 1000


def test_default_is_black_on_plain_paper(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    svg = _svg(http_client, _convert(http_client, image_id))
    assert 'stroke="#000000"' in svg
    assert "<image" not in svg


def test_each_pen_color_maps_to_stroke(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    for color, hexcode in STROKE.items():
        svg = _svg(http_client, _convert(http_client, image_id, line_color=color))
        assert f'stroke="{hexcode}"' in svg, color


def test_white_lines_get_dark_page_rect_without_background(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    svg = _svg(http_client, _convert(http_client, image_id, line_color="white"))
    assert 'stroke="#FFFFFF"' in svg
    assert 'fill="#222222"' in svg


def test_background_is_stretched_full_page_image(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    body = _convert(http_client, image_id, background="dark-texture")
    svg = _svg(http_client, body)
    assert "<image" in svg
    assert 'preserveAspectRatio="none"' in svg
    assert "data:image/jpeg;base64," in svg
    # Full-page rect: A4 portrait 210 x 297 mm.
    assert 'width="210.000" height="297.000"' in svg


def test_requested_combinations(http_client):
    """The exact pairs from the request: white+dark, red/black/blue+light."""
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    pairs = [
        ("white", "dark-texture"),
        ("red", "light-texture"),
        ("black", "light-texture"),
        ("blue", "light-texture"),
    ]
    for color, bg in pairs:
        body = _convert(http_client, image_id, line_color=color, background=bg)
        svg = _svg(http_client, body)
        assert f'stroke="{STROKE[color]}"' in svg, (color, bg)
        assert "<image" in svg and 'preserveAspectRatio="none"' in svg, (color, bg)


def test_display_params_do_not_change_geometry(http_client):
    """Pen/background independence: stats identical across all combos."""
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    baseline = _convert(http_client, image_id)["stats"]
    for color in STROKE:
        for bg in ("none", "dark-texture", "light-texture"):
            stats = _convert(http_client, image_id, line_color=color, background=bg)["stats"]
            assert stats["strokes"] == baseline["strokes"], (color, bg)
            assert stats["pen_down_mm"] == baseline["pen_down_mm"], (color, bg)
            assert stats["points"] == baseline["points"], (color, bg)


def test_invalid_display_params_422(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = default_params("hatch")
    params["line_color"] = "green"
    resp = http_client.post("/v1/convert", json={"image_id": image_id, "params": params})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"
    params = default_params("hatch")
    params["background"] = "beach"
    resp = http_client.post("/v1/convert", json={"image_id": image_id, "params": params})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


def test_penplot_page_exposes_display_controls(http_client):
    resp = http_client.get("/penplot")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="line_color"' in html
    assert 'id="background"' in html
    for color in LINE_COLORS:
        assert f'value="{color}"' in html
    for bid in BACKGROUNDS:
        assert f'value="{bid}"' in html
