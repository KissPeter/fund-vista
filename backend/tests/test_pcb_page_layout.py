"""The pcb page rail: ``layout`` and ``lines`` on ``POST /v1/pcb/render``.

Contract: docs/pcb-page-contract.md (decisions D1 to D10). The render, meta
and ink-split cases run over real HTTP against the session live server. The
ink-split re-plot case needs the sheet evicted from the cache, so it drives
the full app in process (memory cache) with a test client.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.pcb import cache as pcb_cache
from backend.tests.helpers import TEST_HMAC_SECRET

GBR = (Path(__file__).parent / "fixtures" / "pcb_sublayers.gbr").read_bytes()
SPLIT_URL = "/v1/fulfill/ink-split"
LAYERS = {
    "pads": {"enabled": True, "ink": "auto"},
    "tracks": {"enabled": True, "ink": "blue"},
}
PAGE_LANDSCAPE = {"size": "A4", "orientation": "landscape", "margin_mm": 10,
                  "padding_mm": 5, "frame": True, "frame_radius_mm": 2}


def _params(**extra) -> dict:
    return {"paper": "white", "pen": "black", "layers": LAYERS, "mirror": False, **extra}


def _post(client, params: dict):
    return client.post(
        "/v1/pcb/render",
        files={"file": ("board.gbr", GBR, "application/octet-stream")},
        data={"params": json.dumps(params)},
    )


def _render(client, params: dict) -> dict:
    resp = _post(client, params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _label(placement: str, **extra) -> dict:
    return {"placement": placement, "height_mm": 5, **extra}


def test_landscape_is_a_297_by_210_sheet(http_client):
    body = _render(http_client, _params(layout={"page": {"orientation": "landscape"}}))
    root = re.match(r"<svg[^>]*>", body["svg"]).group(0)
    assert 'width="297mm"' in root and 'height="210mm"' in root
    assert 'viewBox="0 0 297 210"' in root


def test_portrait_layout_keeps_the_210_by_297_sheet(http_client):
    body = _render(http_client, _params(layout={"page": {"frame": True}}))
    assert 'viewBox="0 0 210 297"' in body["svg"]


def test_the_artwork_stays_inside_the_page_margins(http_client):
    body = _render(http_client, _params(layout={"page": PAGE_LANDSCAPE}))
    coords = [
        (float(x), float(y))
        for group in re.findall(r'<g id="pcb-[^>]*>(.*?)</g>', body["svg"], re.DOTALL)
        for x, y in re.findall(r"([0-9.]+) ([0-9.]+)", group)
    ]
    assert coords
    assert min(x for x, _ in coords) >= 15 - 0.01
    assert max(x for x, _ in coords) <= 297 - 15 + 0.01
    assert min(y for _, y in coords) >= 15 - 0.01
    assert max(y for _, y in coords) <= 210 - 15 + 0.01


def test_the_frame_is_drawn_only_when_asked(http_client):
    framed = _render(http_client, _params(layout={"page": PAGE_LANDSCAPE}))["svg"]
    bare = _render(http_client, _params(layout={"page": {"orientation": "landscape"}}))["svg"]
    assert 'data-kp-frame="1"' in framed
    assert 'data-kp-frame="1"' not in bare


@pytest.mark.parametrize("placement", ["below", "above"])
def test_outside_title_is_lettering_strokes_without_a_band(http_client, placement):
    body = _render(http_client, _params(
        layout={"page": PAGE_LANDSCAPE, "label": _label(placement)},
        lines=["Main board", "rev 2"],
    ))
    svg = body["svg"]
    assert f'data-kp-label-placement="{placement}"' in svg
    assert 'data-kp-title-block="1"' not in svg
    assert 'data-kp-text="Main board"' in svg and 'data-kp-text="rev 2"' in svg
    assert re.search(r'data-kp-text="Main board"><polyline', svg)


def test_inside_title_has_the_band_and_lettering(http_client):
    svg = _render(http_client, _params(
        layout={"page": PAGE_LANDSCAPE, "label": _label("inside", border=True)},
        lines=["Main board"],
    ))["svg"]
    assert 'data-kp-title-block="1"' in svg
    assert 'data-kp-text="Main board"><polyline' in svg


@pytest.mark.parametrize("placement", ["inside", "below", "above"])
def test_the_plotted_sheet_has_no_text_or_image(http_client, placement):
    svg = _render(http_client, _params(
        layout={"page": PAGE_LANDSCAPE, "label": _label(placement)},
        lines=["Main board", "rev 2"],
    ))["svg"]
    assert not re.search(r"<(?:text|image)[\s>/]", svg)
    assert svg.count("<svg") == 1


def test_lines_alone_draw_a_title_on_the_default_page(http_client):
    body = _render(http_client, _params(lines=["Only a title"]))
    assert 'data-kp-text="Only a title"' in body["svg"]
    assert 'viewBox="0 0 210 297"' in body["svg"]


def test_blank_lines_draw_nothing(http_client):
    body = _render(http_client, _params(
        layout={"page": {"orientation": "landscape"}}, lines=["", "  "],
    ))
    assert "data-kp-label" not in body["svg"]


def test_stats_include_the_furniture_strokes(http_client):
    base = _render(http_client, _params(layout={"page": {"orientation": "landscape"}}))
    framed = _render(http_client, _params(layout={"page": PAGE_LANDSCAPE}))
    titled = _render(http_client, _params(
        layout={"page": PAGE_LANDSCAPE, "label": _label("below")}, lines=["Main board"],
    ))
    assert framed["stats"]["strokes"] == base["stats"]["strokes"] + 1
    assert framed["stats"]["pen_down_mm"] > base["stats"]["pen_down_mm"] + 100
    assert titled["stats"]["strokes"] > framed["stats"]["strokes"] + 1
    assert titled["stats"]["pen_down_mm"] > framed["stats"]["pen_down_mm"]
    assert titled["stats"]["estimated_time_s"] > framed["stats"]["estimated_time_s"]


def test_a_wide_board_is_not_turned_on_a_landscape_sheet(http_client):
    portrait = _render(http_client, _params())
    landscape = _render(http_client, _params(layout={"page": {"orientation": "landscape"}}))
    assert portrait["rotated"] is not landscape["rotated"]


@pytest.mark.parametrize("bad", [
    {"layout": {"page": {}, "label": {}, "extra": 1}},
    {"layout": {"page": {"nope": 1}}},
    {"layout": {"label": {"nope": 1}}},
    {"layout": {"page": {"orientation": "diagonal"}}},
    {"layout": {"page": {"margin_mm": 5}}},
    {"layout": {"page": {"size": "A3"}}},
    {"layout": {"label": {"placement": "left"}}},
    {"lines": ["x" * 42]},
    {"lines": ["a", "b", "c"]},
    {"lines": ["a\x07b"]},
    {"lines": "title"},
])
def test_a_bad_layout_or_lines_is_422_invalid_params(http_client, bad):
    resp = _post(http_client, _params(**bad))
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "invalid_params"


def test_a_41_character_line_is_accepted(http_client):
    assert _post(http_client, _params(lines=["x" * 41, "y" * 41])).status_code == 200


def test_the_cache_key_follows_layout_and_lines(http_client):
    base = _render(http_client, _params())
    landscape = _render(http_client, _params(layout={"page": {"orientation": "landscape"}}))
    titled = _render(http_client, _params(lines=["Main board"]))
    assert len({base["design"]["design_id"], landscape["design"]["design_id"],
                titled["design"]["design_id"]}) == 3
    again = _render(http_client, _params(layout={"page": {"orientation": "landscape"}}))
    assert again["cache_hit"] is True
    assert again["design"]["design_id"] == landscape["design"]["design_id"]


def test_a_default_layout_without_a_title_is_todays_sheet(http_client):
    plain = _render(http_client, _params())
    explicit = _render(http_client, _params(layout={"page": {}, "label": {}}))
    assert explicit["svg"] == plain["svg"]


def test_meta_returns_the_layout_and_lines(http_client):
    params = _params(
        layout={"page": PAGE_LANDSCAPE, "label": _label("below")}, lines=["Main board", "rev 2"],
    )
    design_id = _render(http_client, params)["design"]["design_id"]
    meta = http_client.get(f"/v1/pcb/results/{design_id}/meta")
    assert meta.status_code == 200, meta.text
    got = meta.json()["params"]
    assert got["lines"] == ["Main board", "rev 2"]
    assert got["layout"]["page"]["orientation"] == "landscape"
    assert got["layout"]["page"]["frame"] is True
    assert got["layout"]["label"]["placement"] == "below"
    assert meta.json()["stats"]["strokes"] > 5


def test_meta_of_a_plain_render_has_no_layout_and_no_lines(http_client):
    design_id = _render(http_client, _params())["design"]["design_id"]
    got = http_client.get(f"/v1/pcb/results/{design_id}/meta").json()["params"]
    assert got.get("layout") is None
    assert got.get("lines", []) == []


def test_ink_split_serves_the_page_furniture_in_the_pen_pass(http_client):
    params = _params(layout={"page": PAGE_LANDSCAPE}, lines=["Main board"])
    design = _render(http_client, params)["design"]
    resp = http_client.post(SPLIT_URL, json={"design": design, "kind": "pcb", "params": params})
    assert resp.status_code == 200, resp.text
    files = resp.json()["files"]
    assert [f["ink"] for f in files] == ["black", "blue"]
    assert 'data-kp-frame="1"' in files[0]["svg"]
    assert 'data-kp-text="Main board"' in files[0]["svg"]
    assert "data-kp-frame" not in files[1]["svg"]
    assert 'viewBox="0 0 297 210"' in files[1]["svg"]


def test_ink_split_replots_a_lost_sheet_with_the_layout(monkeypatch):
    from backend.fulfillment import router as fulfill_router
    from backend.main import app
    from backend.pcb import router as pcb_router
    from backend.penplot.config import Settings

    monkeypatch.setenv("PENPIXEL_HMAC_SECRET", TEST_HMAC_SECRET)
    monkeypatch.setattr(pcb_router, "_settings", Settings(allow_svg=True))
    monkeypatch.setattr(fulfill_router, "_settings", Settings(allow_svg=True))
    params = _params(layout={"page": PAGE_LANDSCAPE, "label": _label("below")},
                     lines=["Main board", "rev 2"])
    with TestClient(app) as client:
        pcb_cache.configure_pcb_redis(None)  # the in-process memory cache
        rendered = _render(client, params)
        design = rendered["design"]
        del pcb_cache._cache._memory[pcb_cache.svg_cache_key(design["design_id"])]
        resp = client.post(SPLIT_URL, json={"design": design, "kind": "pcb", "params": params})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "rendered_from_stored_source" in body["warnings"]
    assert body["design_id"] == design["design_id"]
    black = body["files"][0]["svg"]
    assert 'viewBox="0 0 297 210"' in black
    assert 'data-kp-frame="1"' in black and 'data-kp-text="rev 2"' in black
