"""Unit tests for imaging hardening: decompression bombs (D.3.2) and the SVG
ingestion contract (4-tuple result + warnings wiring, D.3.3, C.2.5)."""

from __future__ import annotations

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