"""PCB Gerber preview: single-file SVG over the live HTTP server.

Hermetic: parsing is local CPU (gerbonara), no upstream I/O.
"""

from __future__ import annotations

from pathlib import Path

from backend.pcb.render import MAX_UPLOAD_BYTES, _strip_unsafe

FIXTURES = Path(__file__).parent / "fixtures"
URL = "/v1/pcb/preview"


def _post(http_client, name: str, payload: bytes):
    return http_client.post(URL, files={"file": (name, payload, "application/octet-stream")})


def _assert_clean_svg(resp) -> None:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("image/svg+xml")
    body = resp.text
    assert "<svg" in body
    assert "<image" not in body
    assert "<text" not in body


def test_gerber_single_file_renders_svg(http_client):
    resp = _post(http_client, "top.gbr", (FIXTURES / "pcb_minimal.gbr").read_bytes())
    _assert_clean_svg(resp)
    assert 'stroke="black"' in resp.text or 'fill="black"' in resp.text


def test_excellon_drill_file_renders_svg(http_client):
    resp = _post(http_client, "drill.drl", (FIXTURES / "pcb_minimal.drl").read_bytes())
    _assert_clean_svg(resp)


def test_oversize_upload_is_413(http_client):
    payload = b"G04 pad*\n" + b"X0Y0D02*\n" * ((MAX_UPLOAD_BYTES // 9) + 1)
    assert len(payload) > MAX_UPLOAD_BYTES
    resp = _post(http_client, "big.gbr", payload)
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


def test_non_gerber_text_is_422(http_client):
    resp = _post(http_client, "notes.txt", b"hello world, this is not a PCB file\n")
    assert resp.status_code == 422
    assert "Gerber or Excellon" in resp.json()["error"]["message"]


def test_empty_upload_is_422(http_client):
    resp = _post(http_client, "empty.gbr", b"")
    assert resp.status_code == 422
    assert "empty" in resp.json()["error"]["message"]


def test_strip_removes_image_and_text_elements():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<g><path d="M0 0 L1 1" stroke="black"/>'
        '<text x="1" y="1">label</text>'
        '<image href="x.png"/></g></svg>'
    )
    out = _strip_unsafe(svg)
    assert "<path" in out
    assert "<text" not in out
    assert "<image" not in out
