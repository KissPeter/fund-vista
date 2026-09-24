"""Unit tests for imaging hardening: decompression bombs (D.3.2) and the SVG
ingestion contract (4-tuple result + warnings wiring, D.3.3, C.2.5)."""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from PIL import Image as PILImage

from backend.penplot import imaging
from backend.penplot.errors import PenPlotError
from backend.tests.helpers import png_bytes


def _svg(inner: str) -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="210" height="297">'
        f"{inner}</svg>"
    ).encode()


def test_load_raster_decompression_bomb_is_413(monkeypatch):
    # Review D.3.2: a huge-dimension image must surface as a 413 envelope, not
    # an opaque PIL crash or a silent memory blowup.
    monkeypatch.setattr(PILImage, "MAX_IMAGE_PIXELS", 200)
    with pytest.raises(PenPlotError) as ei:
        imaging.load_raster(png_bytes())
    assert ei.value.status == 413
    assert ei.value.code == "image_too_large"


def test_sniff_extension_decompression_bomb_is_413(monkeypatch):
    monkeypatch.setattr(PILImage, "MAX_IMAGE_PIXELS", 200)
    with pytest.raises(PenPlotError) as ei:
        imaging.sniff_extension(png_bytes(), "photo.png")
    assert ei.value.status == 413
    assert ei.value.code == "image_too_large"


def _png_with_svg_metadata() -> bytes:
    import io

    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text(
        "XML:com.adobe.xmp",
        '<x:xmpmeta xmlns:svg="http://www.w3.org/2000/svg"><svg:Desc/></x:xmpmeta>',
    )
    img = PILImage.new("RGB", (64, 64))
    buf = io.BytesIO()
    img.save(buf, format="PNG", pnginfo=info)
    return buf.getvalue()


def test_sniff_extension_raster_with_svg_metadata_is_not_svg():
    """Regression: a valid PNG whose metadata mentions `<svg` must sniff as a
    raster, not as SVG — otherwise upload 400s with the bogus
    "Uploaded SVG is not well-formed XML." error."""
    data = _png_with_svg_metadata()
    assert b"<svg" in data.lstrip()[:2048].lower()  # naive check WOULD trip
    assert imaging.sniff_extension(data, "photo.png") == "png"


def test_sniff_extension_genuine_svg_still_detected():
    assert imaging.sniff_extension(_svg('<rect x="0" y="0" width="10" height="10"/>'), "shapes.svg") == "svg"
    # Content wins even when the filename does NOT claim svg.
    assert imaging.sniff_extension(_svg('<rect x="0" y="0" width="10" height="10"/>'), "image") == "svg"


def test_sniff_extension_name_svg_still_routes_malformed_to_svg_parser():
    # A broken `.svg` keeps its honest 400 path (parser, not a 422).
    assert imaging.sniff_extension(b"<svg not well-formed <svg", "broken.svg") == "svg"


def test_parse_svg_vectors_returns_four_tuple_with_warnings():
    polylines, w, h, warnings = imaging.parse_svg_vectors(_svg('<rect x="0" y="0" width="10" height="10"/>'))
    assert len(polylines) == 1
    assert (w, h) == (210.0, 297.0)
    assert warnings == []


def test_parse_embedded_raster_yields_warning_and_no_polylines():
    inner = (
        '<rect x="0" y="0" width="100" height="100"/>'
        '<image href="data:image/png;base64,x" x="0" y="0" width="10" height="10"/>'
    )
    polylines, w, h, warnings = imaging.parse_svg_vectors(_svg(inner))
    assert warnings == [imaging.WARNING_EMBEDDED_RASTERS_IGNORED]
    assert (w, h) == (210.0, 297.0)
    assert len(polylines) == 1  # only the rect is plottable; image adds nothing


def test_parse_embedded_raster_via_use_yields_warning():
    inner = (
        '<rect x="0" y="0" width="100" height="100"/>'
        '<defs><image id="i" href="data:image/png;base64,x" width="10" height="10"/></defs>'
        '<use href="#i"/>'
    )
    polylines, _, _, warnings = imaging.parse_svg_vectors(_svg(inner))
    assert imaging.WARNING_EMBEDDED_RASTERS_IGNORED in warnings
    assert len(polylines) == 1


def test_entity_expansion_bomb_rejected():
    bomb = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE svg [<!ENTITY a "AAAAAAAAAAAAAAAAAAAA">]>'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="210" height="297">'
        b'<rect x="0" y="0" width="100" height="100" fill="&a;"/></svg>'
    )
    with pytest.raises(PenPlotError) as ei:
        imaging.parse_svg_vectors(bomb)
    assert ei.value.status == 400
    assert ei.value.code == "bad_image"


def test_pathological_nesting_rejected():
    deep = _svg("<g>" * 300 + '<rect x="0" y="0" width="1" height="1"/>' + "</g>" * 300)
    with pytest.raises(PenPlotError) as ei:
        imaging.parse_svg_vectors(deep)
    assert ei.value.status == 400
    assert ei.value.code == "bad_image"


def _hatched_and_block_mask(h: int = 120, w: int = 120) -> np.ndarray:
    """Synthetic ink mask: a uniform thin 45° hatch everywhere, plus one
    thick (8px) solid rectangle ring standing in for real linework."""
    gray = np.full((h, w), 255, dtype=np.uint8)
    for offset in range(-h, w, 3):
        cv2.line(gray, (offset, 0), (offset + h, h), color=0, thickness=1)
    cv2.rectangle(gray, (30, 30), (90, 90), color=0, thickness=8)
    return gray < 128


def test_strip_hatch_zero_is_identity():
    mask = _hatched_and_block_mask()
    assert (imaging.strip_hatch(mask, 0) == mask).all()


def test_strip_hatch_erases_thin_lines_keeps_thick_strokes():
    mask = _hatched_and_block_mask()
    stripped = imaging.strip_hatch(mask, 5)

    # A pure-hatch corner (far from the thick block) is fully erased.
    assert stripped[0:20, 0:20].sum() == 0
    # The thick block's ring mostly survives (opening only trims corners).
    top_edge = stripped[30:38, 30:90]
    assert top_edge.mean() > 0.5
    # Overall ink drops sharply once the (much more numerous) hatch pixels
    # are gone, even though the block itself is preserved.
    assert int(stripped.sum()) < int(mask.sum()) * 0.5
