"""POST /v1/compose-sheet with preview="png": size, aspect, colours, cache and guards.

Pixel checks decode the PNG with Pillow (already a dependency). The SVG default
and the inline order output are pinned by digest in
``test_penplot_compose_sheet.py``; the byte-identity checks here cover the new
fields left at their defaults.
"""

from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image

from backend.penplot import colour_bake, raster_out
from backend.penplot import compose_sheet as cs
from backend.penplot.errors import PenPlotError
from backend.penplot.schemas import ComposeSheetRequest

BOX_W, BOX_H = 3840, 2160  # 4K screen: the fixed fit box

ART = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100">'
    '<g id="runways" fill="none" stroke="#000000"><path d="M 0 80 L 200 80" stroke-width="6"/></g>'
    '<rect x="0" y="0" width="200" height="60" fill="#000000"/>'
    "</svg>"
)


def _req(**kw) -> ComposeSheetRequest:
    base: dict = {"svg": ART}
    base.update(kw)
    return ComposeSheetRequest.model_validate(base)


def _png(body: ComposeSheetRequest) -> Image.Image:
    data = cs._render_preview(body, cs.get_settings())
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def _sample(img: Image.Image, fx: float, fy: float) -> tuple[int, int, int]:
    """RGB at a fractional position of the canvas (0..1 each axis)."""
    px = img.convert("RGB").getpixel((int(img.width * fx), int(img.height * fy)))
    return px[0], px[1], px[2]


# -- type, size and aspect ---------------------------------------------------


def test_portrait_sheet_fits_the_box_touching_the_height():
    img = _png(_req(preview="png"))
    assert img.format == "PNG"
    assert img.height == BOX_H
    assert img.width <= BOX_W
    assert abs(img.width / img.height - 210 / 297) < 0.001


def test_landscape_sheet_fits_the_box_touching_the_height():
    img = _png(_req(preview="png", layout={"page": {"orientation": "landscape"}}))
    assert img.height == BOX_H
    assert img.width <= BOX_W
    assert abs(img.width / img.height - 297 / 210) < 0.001


def test_preview_size_fits_the_box_and_touches_one_edge():
    w, h = raster_out.preview_size(210, 297, BOX_W, BOX_H)
    assert (w, h) == (1527, 2160)
    w, h = raster_out.preview_size(297, 210, BOX_W, BOX_H)
    assert (w, h) == (3055, 2160)
    # A square fits by its height, the other side stays inside.
    assert raster_out.preview_size(100, 100, BOX_W, BOX_H) == (2160, 2160)


def test_box_bounds_are_set_in_config():
    from pydantic import ValidationError  # noqa: PLC0415

    from backend.penplot.config import Settings  # noqa: PLC0415

    cfg = Settings()
    assert (cfg.preview_png_box_w_px, cfg.preview_png_box_h_px) == (BOX_W, BOX_H)
    with pytest.raises(ValidationError):
        Settings(preview_png_box_w_px=8000)


def test_preview_size_rejects_non_positive_sheet():
    with pytest.raises(ValueError):
        raster_out.preview_size(0, 297, BOX_W, BOX_H)


# -- colours by pixel sampling -----------------------------------------------


def test_pen_colour_is_baked_into_the_furniture_and_artwork():
    img = _png(_req(preview="png", pen="#ff0000", paper="#ffffff", lines=["Title"]))
    # The artwork box sits at y 106..196 mm of 297 mm (image mode, default
    # layout); its black bar is the top 60 % of that box.
    assert _sample(img, 0.5, 0.42) == (255, 0, 0)
    # Paper stays white where nothing is drawn (top of the sheet).
    assert _sample(img, 0.5, 0.05) == (255, 255, 255)
    # The title lettering is drawn in the pen colour (bottom of the sheet).
    assert _red_pixels(img, 0.90, 0.98) > 0


def test_runway_colour_is_baked_into_runway_elements_only():
    img = _png(_req(preview="png", pen="#0000ff", runway="#00ff00", paper="#ffffff"))
    # Runway line sits at y 80 of 100 in the artwork box: 106 + 0.8 * 90 mm,
    # i.e. about 0.60 of the sheet height (stroke 6 units wide).
    assert _has_pixel(img, 0.5, 0.57, 0.62, (0, 255, 0))
    # The bar above it is the pen colour, not the runway colour.
    assert _sample(img, 0.5, 0.42) == (0, 0, 255)


def _red_pixels(img: Image.Image, y0: float, y1: float) -> int:
    rgb = img.convert("RGB")
    count = 0
    for y in range(int(img.height * y0), int(img.height * y1), 3):
        for x in range(0, img.width, 3):
            r, g, b = rgb.getpixel((x, y))
            if r > 200 and g < 60 and b < 60:
                count += 1
    return count


def _has_pixel(img: Image.Image, fx: float, y0: float, y1: float, want) -> bool:
    rgb = img.convert("RGB")
    x = int(img.width * fx)
    return any(rgb.getpixel((x, y)) == want for y in range(int(img.height * y0), int(img.height * y1)))


def test_dark_paper_is_the_canvas_and_pen_reads_light():
    img = _png(_req(preview="png", pen="#f2f2f2", paper="#000000"))
    assert _sample(img, 0.5, 0.05) == (0, 0, 0)
    assert _sample(img, 0.5, 0.42) == (242, 242, 242)


def test_default_paper_is_white_when_none_is_given():
    img = _png(_req(preview="png"))
    assert _sample(img, 0.5, 0.05) == (255, 255, 255)


# -- recolour rules -----------------------------------------------------------


def test_recolour_without_colours_returns_the_input_unchanged():
    assert colour_bake.recolour_artwork(ART) is ART


def test_recolour_keeps_white_none_and_url_paint():
    src = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<rect fill="#ffffff" stroke="none"/><rect fill="url(#g)"/>'
        '<path stroke="#000000"/></svg>'
    )
    out = colour_bake.recolour_artwork(src, pen="#123456")
    assert 'fill="#ffffff"' in out
    assert 'stroke="none"' in out
    assert 'fill="url(#g)"' in out
    assert 'stroke="#123456"' in out


def test_recolour_reaches_children_of_a_runway_group():
    src = (
        '<svg xmlns="http://www.w3.org/2000/svg"><g id="osm-runways">'
        '<path stroke="#000000"/></g><path stroke="#000000"/></svg>'
    )
    out = colour_bake.recolour_artwork(src, pen="#111111", runway="#222222")
    assert out.count('stroke="#222222"') == 1
    assert out.count('stroke="#111111"') == 1


# -- defaults keep the SVG and the order file byte-identical ------------------


def test_svg_default_is_byte_identical_to_an_explicit_svg_request():
    default = cs.compose_sheet_svg(_req(), max_svg_bytes=10**7)
    explicit = cs.compose_sheet_svg(_req(preview="svg"), max_svg_bytes=10**7)
    assert default == explicit


def test_inline_order_file_is_unchanged_by_the_new_fields_at_default():
    base = cs.compose_sheet_svg(_req(artwork="inline", lines=["T"]), max_svg_bytes=10**7)
    again = cs.compose_sheet_svg(
        _req(artwork="inline", lines=["T"], preview="svg"), max_svg_bytes=10**7,
    )
    assert hashlib.sha256(base.encode()).hexdigest() == hashlib.sha256(again.encode()).hexdigest()


def test_pen_applies_to_the_svg_preview_only_when_given():
    plain = cs.compose_sheet_svg(_req(lines=["T"]), max_svg_bytes=10**7)
    pen = cs.compose_sheet_svg(_req(lines=["T"], pen="#ff0000"), max_svg_bytes=10**7)
    assert plain != pen
    # Furniture strokes carry the pen colour in plain markup.
    assert 'stroke="#ff0000"' in pen


# -- validation ----------------------------------------------------------------


@pytest.mark.parametrize("bad", ["red", "#fff", "#gggggg", "#ff00000"])
def test_colour_fields_accept_only_six_digit_hex(bad):
    with pytest.raises(ValueError):
        _req(pen=bad)


def test_preview_rejects_unknown_values_and_extra_fields():
    with pytest.raises(ValueError):
        _req(preview="jpeg")
    with pytest.raises(ValueError):
        _req(width_px=4000)


def test_external_reference_in_artwork_is_not_loaded_by_the_renderer():
    # The artwork is embedded as a data URI and resvg gets no resources_dir,
    # so a file:// href inside it is never read: the render still succeeds.
    bad = ART.replace("</svg>", '<image href="file:///etc/hosts" width="1" height="1"/></svg>')
    img = _png(_req(svg=bad, preview="png"))
    assert img.format == "PNG"


def test_render_refuses_malformed_svg():
    with pytest.raises(PenPlotError):
        raster_out.render_png("<svg><unclosed>", 100, 100, None)


def test_pixel_root_rewrites_only_the_root_size():
    svg = '<svg width="210mm" height="297mm" viewBox="0 0 210 297"><rect stroke-width="2"/></svg>'
    out = raster_out._pixel_root(svg, 100, 200)
    assert 'width="100"' in out and 'height="200"' in out
    assert 'viewBox="0 0 210 297"' in out
    assert 'stroke-width="2"' in out


# -- HTTP: PNG response, GET route, cache, filename gate ---------------------


PNG_BODY = {"svg": ART, "lines": ["Title"], "preview": "png", "pen": "#112233"}


def test_http_png_response_headers_and_cache(http_client):
    resp = http_client.post("/v1/compose-sheet", json=PNG_BODY)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("image/png")
    assert "immutable" in resp.headers["cache-control"]
    assert resp.headers["x-preview-width"] and resp.headers["x-preview-height"]
    url = resp.headers["x-preview-url"]
    path = url[url.index("/v1/previews/"):]
    again = http_client.get(path)
    assert again.status_code == 200
    assert again.headers["content-type"].startswith("image/png")
    assert "immutable" in again.headers["cache-control"]
    assert again.content == resp.content


def test_http_png_second_identical_request_is_served_from_cache(http_client):
    first = http_client.post("/v1/compose-sheet", json=PNG_BODY)
    second = http_client.post("/v1/compose-sheet", json=PNG_BODY)
    assert first.headers["etag"] == second.headers["etag"]
    assert first.content == second.content


@pytest.mark.parametrize("name", [
    "../secrets_preview.png", "abc_preview.png", "a" * 64 + ".png", "x" * 64 + "_preview.svg",
])
def test_http_preview_filename_gate_refuses_other_names(http_client, name):
    assert http_client.get(f"/v1/previews/{name}").status_code == 404


def test_http_svg_default_still_answers_svg(http_client):
    resp = http_client.post("/v1/compose-sheet", json={"svg": ART})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
