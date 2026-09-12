"""Real-HTTP tests for full vpype-`read` parity: quantization, transforms,
viewBox mapping, extended path commands, <text> outlining, <use>, and
display/visibility filtering. Same live-server fixture as the core suite.
"""

from __future__ import annotations

import re

from backend.tests.helpers import default_params, upload

RECT = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="210" height="297">'
    '<rect x="10" y="10" width="20" height="10"/></svg>'
).encode()


def _svg_bytes(inner: str, attrs: str = 'width="210" height="297"') -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" {attrs}>{inner}</svg>'
    ).encode()


def _convert_ok(http_client, svg: bytes, method: str = "contour") -> dict:
    image_id = upload(http_client, svg, "v.svg").json()["image_id"]
    resp = http_client.post(
        "/v1/convert", json={"image_id": image_id, "params": default_params(method)}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _fetch_svg(http_client, body: dict) -> str:
    resp = http_client.get(body["svg_url"])
    assert resp.status_code == 200
    assert "image/svg+xml" in resp.headers["content-type"]
    return resp.text


def _coords(svg_text: str) -> list[float]:
    return [float(n) for n in re.findall(r"M ([\d.\-]+) ([\d.\-]+)|L ([\d.\-]+) ([\d.\-]+)", svg_text)
            for n in n if n]


def test_output_coordinates_are_quantized(http_client):
    """vpype `read --quantization 0.02mm`: every emitted coord snaps to grid."""
    body = _convert_ok(http_client, RECT)
    for c in _coords(_fetch_svg(http_client, body)):
        assert abs(c * 50 - round(c * 50)) < 1e-6, c


def test_quantized_raster_output(http_client):
    from backend.tests.helpers import png_bytes

    image_id = upload(http_client, png_bytes()).json()["image_id"]
    resp = http_client.post(
        "/v1/convert", json={"image_id": image_id, "params": default_params("hatch")}
    )
    for c in _coords(_fetch_svg(http_client, resp.json())):
        assert abs(c * 50 - round(c * 50)) < 1e-6, c


def test_transform_equivalence(http_client):
    """translate/scale groups must equal pre-transformed geometry byte-for-byte."""
    reference = _fetch_svg(http_client, _convert_ok(http_client, RECT))
    moved = _svg_bytes('<g transform="translate(10,10)"><rect x="0" y="0" width="20" height="10"/></g>')
    scaled = _svg_bytes('<g transform="scale(2)"><rect x="5" y="5" width="10" height="5"/></g>')
    nested = _svg_bytes(
        '<g transform="translate(5 5)"><g transform="scale(2) translate(2.5,2.5)">'
        '<rect x="0" y="0" width="10" height="5"/></g></g>'
    )
    assert _fetch_svg(http_client, _convert_ok(http_client, moved)) == reference
    assert _fetch_svg(http_client, _convert_ok(http_client, scaled)) == reference
    assert _fetch_svg(http_client, _convert_ok(http_client, nested)) == reference


def test_rotate_and_matrix_transforms(http_client):
    inner = (
        '<g transform="rotate(90 20 15)"><rect x="10" y="10" width="20" height="10"/></g>'
        '<g transform="matrix(1,0,0,1,0,0)"><circle cx="100" cy="100" r="15"/></g>'
    )
    body = _convert_ok(http_client, _svg_bytes(inner))
    assert body["stats"]["strokes"] >= 2
    assert "<path" in _fetch_svg(http_client, body)


def test_viewbox_mapping(http_client):
    """viewBox 100 -> 200px viewport must equal a 2x pre-scaled document."""
    reference = _fetch_svg(http_client, _convert_ok(http_client, RECT))
    assert reference == _fetch_svg(
        http_client,
        _convert_ok(
            http_client,
            _svg_bytes(
                '<rect x="10" y="10" width="20" height="10"/>',
                attrs='width="420" height="594" viewBox="0 0 210 297"',
            ),
        ),
    )
    # Non-uniform viewBox (xMidYMid meet letterboxes) still converts sanely.
    body = _convert_ok(
        http_client,
        _svg_bytes(
            '<rect x="0" y="0" width="100" height="50"/>',
            attrs='width="200" height="200" viewBox="0 0 100 50"',
        ),
    )
    assert body["stats"]["pen_down_mm"] > 0


def test_arc_smooth_and_relative_commands(http_client):
    d = (
        "M50,0 A50,50 0 0,1 0,50 A50,50 0 0,1 -50,0 "
        "A50,50 0 0,1 0,-50 A50,50 0 0,1 50,0 Z "
        "m120 0 c10 0 20 10 30 10 s20-10 30-10 q10 10 20 0 t20 0"
    )
    body = _convert_ok(http_client, _svg_bytes(f'<path d="{d}"/>'))
    assert body["stats"]["strokes"] >= 2
    assert body["stats"]["pen_down_mm"] > 0


def test_text_outlines(http_client):
    body = _convert_ok(
        http_client,
        _svg_bytes('<text x="10" y="100" font-size="40">Hi</text>'),
    )
    assert body["stats"]["strokes"] >= 2  # H + dot of i, at minimum
    assert "<path" in _fetch_svg(http_client, body)
    # Anchor + tspan stacking convert without errors.
    body = _convert_ok(
        http_client,
        _svg_bytes(
            '<text x="105" y="50" font-size="20" text-anchor="middle">A'
            '<tspan x="105" dy="24">B</tspan></text>'
        ),
    )
    assert body["stats"]["strokes"] >= 2


def test_hidden_elements_skipped(http_client):
    reference = _fetch_svg(http_client, _convert_ok(http_client, RECT))
    inner = (
        '<rect x="10" y="10" width="20" height="10"/>'
        '<rect x="50" y="50" width="5" height="5" display="none"/>'
        '<circle cx="100" cy="100" r="10" visibility="hidden"/>'
        '<g style="display:none"><rect x="1" y="1" width="3" height="3"/></g>'
    )
    assert _fetch_svg(http_client, _convert_ok(http_client, _svg_bytes(inner))) == reference


def test_use_reference(http_client):
    reference = _fetch_svg(http_client, _convert_ok(http_client, RECT))
    inner = (
        '<defs><rect id="r" x="0" y="0" width="20" height="10"/></defs>'
        '<use href="#r" x="10" y="10"/>'
    )
    assert _fetch_svg(http_client, _convert_ok(http_client, _svg_bytes(inner))) == reference


def test_css_units_and_rounded_rect(http_client):
    body = _convert_ok(
        http_client,
        _svg_bytes('<rect x="10mm" y="10mm" width="20mm" height="10mm" rx="2mm"/>'),
    )
    assert body["stats"]["pen_down_mm"] > 0
    assert "<path" in _fetch_svg(http_client, body)


def test_upload_rejects_entity_expansion_bomb(http_client):
    """Review D.3.3: an internal-entity DTD must 400, never expand (billion-laughs)."""
    bomb = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE svg [<!ENTITY a "AAAAAAAAAAAAAAAAAAAA">]>'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="210" height="297">'
        b'<rect x="0" y="0" width="100" height="100" fill="&a;"/></svg>'
    )
    resp = upload(http_client, bomb, "bomb.svg")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_image"


def test_upload_rejects_pathological_nesting(http_client):
    """Review D.3.3: a nesting-depth cap must 400; deep <g> trees cannot recurse."""
    deep = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        + "<g>" * 300 + '<rect x="0" y="0" width="1" height="1"/>' + "</g>" * 300
        + "</svg>"
    )
    resp = upload(http_client, deep.encode(), "deep.svg")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_image"


def test_upload_svg_with_embedded_raster_warns(http_client):
    """Review C.2.5: `<image>` is not plot geometry — ignore it, but say so."""
    inner = (
        '<rect x="0" y="0" width="100" height="100"/>'
        '<image href="data:image/png;base64,x" x="0" y="0" width="10" height="10"/>'
    )
    resp = upload(http_client, _svg_bytes(inner), "mixed.svg")
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == ["embedded_rasters_ignored"]
    assert resp.json()["is_vector"] is True
    # The same warning survives a convert round-trip.
    image_id = resp.json()["image_id"]
    resp = http_client.post(
        "/v1/convert", json={"image_id": image_id, "params": default_params("hatch")}
    )
    assert resp.status_code == 200, resp.text
    assert "embedded_rasters_ignored" in resp.json()["warnings"]
