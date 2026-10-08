"""Wordmark tests: geometry units + real-HTTP render/import/results.

Geometry cases are pure units (no server). HTTP cases go through a live
uvicorn socket via the ``http_client`` fixture — no TestClient, no
in-process ASGI shortcuts. Rendering is local CPU (no upstream), so every
case here is hermetic.
"""

from __future__ import annotations

import re

import pytest

from backend.penplot.errors import PenPlotError
from backend.wordmark.geometry import auto_cuts, geometry_source_version, render_wordmark
from backend.wordmark.schemas import CutSpec, RenderRequest

HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ------------------------------------------------------------- geometry ---


def test_render_is_deterministic():
    first = render_wordmark("SPACE", "poppins-bold", "solid", -0.12, 0.04, None, 1000)
    second = render_wordmark("SPACE", "poppins-bold", "solid", -0.12, 0.04, None, 1000)
    assert first[0] == second[0]
    assert first[1] == second[1]


def test_render_counts_and_svg_shape():
    svg, path_counts, raw_counts, applied, warnings = render_wordmark(
        "SPACE", "poppins-bold", "solid", -0.12, 0.04, None, 1000
    )
    assert path_counts == {"letters": 5, "cuts": len(applied)}
    assert len(applied) >= 1  # auto cuts: max(1, letters // 3)
    assert warnings == []
    assert raw_counts["contours"] > 0
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert 'width="1000"' in svg
    assert 'fill-rule="nonzero"' in svg


def test_modes_all_render():
    for mode in ("solid", "knockout", "layered"):
        svg, path_counts, _raw, applied, _w = render_wordmark(
            "PEN PIXEL", "poppins-bold", mode, -0.12, 0.04, None, 800
        )
        assert path_counts["letters"] == 8
        assert len(applied) >= 1
        assert "<path" in svg


def test_explicit_cut_applies_and_space_cut_skips():
    _svg, _pc, _rc, applied, warnings = render_wordmark(
        "HELLO", "poppins-bold", "solid", -0.12, 0.04,
        [CutSpec(junction=1, y=0.5, r=0.2)], 1000,
    )
    assert [(c.junction, c.y, c.r) for c in applied] == [(1, 0.5, 0.2)]
    assert warnings == []

    _svg, _pc, _rc, applied, warnings = render_wordmark(
        "A B", "poppins-bold", "solid", -0.12, 0.04,
        [CutSpec(junction=1, y=0.5, r=0.2)], 1000,
    )
    assert applied == []
    assert warnings == ["cut_skipped_space"]


def test_missing_glyph_rejected():
    with pytest.raises(PenPlotError) as exc:
        render_wordmark("A😀B", "poppins-bold", "solid", -0.12, 0.04, None, 100)
    assert exc.value.code == "invalid_params"


def test_spaces_only_rejected():
    with pytest.raises(PenPlotError) as exc:
        render_wordmark("   ", "poppins-bold", "solid", -0.12, 0.04, None, 100)
    assert exc.value.code == "invalid_params"


def test_control_characters_rejected():
    with pytest.raises(PenPlotError) as exc:
        render_wordmark("A\x00B", "poppins-bold", "solid", -0.12, 0.04, None, 100)
    assert exc.value.code == "invalid_params"


def test_unknown_font_rejected():
    with pytest.raises(PenPlotError) as exc:
        render_wordmark("AB", "comic-sans", "solid", -0.12, 0.04, None, 100)
    assert exc.value.code == "invalid_params"


def test_auto_cuts_bounded():
    # Every junction lands inside the text, y/r inside their ranges —
    # including long inputs (the reference CLI overruns its digest here).
    for text in ("A", "AB", "HELLO WORLD", "X" * 24, "PEN PIXEL SHOP 123"):
        for j, y, r in auto_cuts(text):
            assert 1 <= j <= max(1, len(text) - 1)
            assert 0.0 <= y <= 1.0
            assert 0.05 <= r <= 0.4


def test_geometry_version_pins_cache():
    assert re.fullmatch(r"[0-9a-f]{12}", geometry_source_version())


# -------------------------------------------------------------- schemas ---


def test_schema_rejects_cut_past_last_gap():
    with pytest.raises(Exception):
        RenderRequest(text="AB", cuts=[CutSpec(junction=5, y=0.5, r=0.2)])


def test_schema_rejects_empty_cuts_and_extra_keys():
    with pytest.raises(Exception):
        RenderRequest(text="AB", cuts=[])
    with pytest.raises(Exception):
        RenderRequest(text="AB", typo_field=1)  # type: ignore[call-arg]


# ----------------------------------------------------------------- HTTP ---


def test_render_returns_preview_and_caches(http_client):
    body = {"text": "SPACE"}
    first = http_client.post("/v1/wordmark/render", json=body)
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["text"] == "SPACE"
    assert data["mode"] == "solid"
    assert data["path_counts"]["letters"] == 5
    assert len(data["cuts_applied"]) >= 1
    assert data["cache_hit"] is False
    assert data["svg_url"].startswith("http")
    assert "/v1/wordmark/results/" in data["svg_url"]

    second = http_client.post("/v1/wordmark/render", json=body)
    assert second.status_code == 200, second.text
    assert second.json()["cache_hit"] is True
    assert second.json()["svg_url"] == data["svg_url"]


def test_results_serves_svg(http_client):
    svg_url = http_client.post("/v1/wordmark/render", json={"text": "AB"}).json()["svg_url"]
    token = svg_url.rsplit("/", 1)[-1]
    resp = http_client.get(f"/v1/wordmark/results/{token}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert resp.text.startswith("<svg")


def test_results_unknown_token_404(http_client):
    resp = http_client.get(f"/v1/wordmark/results/{'0' * 40}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "result_not_found"


def test_import_registers_image_id(http_client):
    resp = http_client.post("/v1/wordmark/import", json={"text": "AB", "mode": "layered"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert HEX64.fullmatch(data["image_id"])
    assert data["mode"] == "layered"
    assert data["path_counts"]["letters"] == 2
    assert "svg_url" not in data


def test_imported_image_converts(http_client):
    """The import → convert chain works like an uploaded SVG (vector branch)."""
    image_id = http_client.post("/v1/wordmark/import", json={"text": "HI"}).json()["image_id"]
    token = http_client.post("/v1/tokens", json={"image_id": image_id})
    assert token.status_code == 200, token.text
    converted = http_client.post(
        "/v1/convert",
        json={"image_id": token.json()["design_id"], "params": {"page": {"size": "A4"}}},
    )
    assert converted.status_code == 200, converted.text
    assert "/v1/results/" in converted.json()["svg_url"]


def test_render_rejects_bad_input(http_client):
    # Empty text: schema gate.
    resp = http_client.post("/v1/wordmark/render", json={"text": ""})
    assert resp.status_code == 422
    # Cut past the last gap: schema gate.
    resp = http_client.post(
        "/v1/wordmark/render",
        json={"text": "AB", "cuts": [{"junction": 9, "y": 0.5, "r": 0.2}]},
    )
    assert resp.status_code == 422
    # Missing glyph: geometry gate, same envelope.
    resp = http_client.post("/v1/wordmark/render", json={"text": "A😀B"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"
    # Unknown field: extra="forbid" gate.
    resp = http_client.post("/v1/wordmark/render", json={"text": "AB", "nope": 1})
    assert resp.status_code == 422
