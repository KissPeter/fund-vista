"""PCB render: one file on one A4 sheet (paper and pen rule, fit, stats,
ceiling, token). Unit cases compose in-process; HTTP cases go through the
live server."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.fulfillment.ink_split import INK_HEX
from backend.pcb import sheet as sheet_mod
from backend.pcb.layers import board_shapes
from backend.pcb.schemas import PcbRenderParams
from backend.pcb.sheet import compose_sheet
from backend.penplot.errors import PenPlotError

FIXTURES = Path(__file__).parent / "fixtures"
URL = "/v1/pcb/render"
SUBLAYER_GBR = (FIXTURES / "pcb_sublayers.gbr").read_bytes()

# An L of two tracks: (0,0)-(10,0) then (10,0)-(10,5). Board 10 x 5 mm, so it
# fits A4 rotated at 27.7x: 15 mm of track becomes 415.5 mm of pen-down.
L_TRACKS = (
    b"G04 L-shaped track*\n"
    b"%TF.FileFunction,Copper,L1,Top*%\n"
    b"%FSLAX26Y26*%\n%MOMM*%\n%ADD10C,0.250*%\n"
    b"G75*\nG01*\nD10*\n"
    b"X0Y0D02*\nX10000000Y0D01*\n"
    b"X10000000Y0D02*\nX10000000Y5000000D01*\n"
    b"M02*\n"
)


def _all_on(**inks: str) -> PcbRenderParams:
    layers = {
        layer: {"enabled": True, "ink": inks.get(layer, "auto")}
        for layer in ("pads", "tracks", "pours")
    }
    return PcbRenderParams(paper="white", pen="black", layers=layers)


def _compose(data: bytes, params: PcbRenderParams):
    return compose_sheet(board_shapes(data), params)


def _points(svg: str) -> list[tuple[float, float]]:
    return [(float(x), float(y)) for x, y in re.findall(r"[ML]([-\d.]+) ([-\d.]+)", svg)]


def test_l_tracks_are_fitted_rotated_with_exact_stats():
    res = _compose(L_TRACKS, PcbRenderParams(layers={"tracks": {"enabled": True}}))
    assert res.rotated is True
    assert res.stats.strokes == 2
    assert res.stats.pen_down_mm == pytest.approx(415.5, abs=0.01)
    assert res.stats.pen_up_mm == pytest.approx(0.0, abs=0.01)
    # 415.5 mm at 2000 mm/min, plus 2 pen lifts of 0.3 s.
    assert res.stats.estimated_time_s == pytest.approx(13.1, abs=0.05)
    assert [(layer.id, layer.paths) for layer in res.layers] == [("tracks", 2)]


def test_sheet_stays_inside_a4_with_a_10_mm_margin():
    svg = _compose(SUBLAYER_GBR, _all_on()).svg
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm"')
    for x, y in _points(svg):
        assert 10 - 1e-6 <= x <= 200 + 1e-6
        assert 10 - 1e-6 <= y <= 287 + 1e-6


def test_mirror_reflects_the_sheet_about_its_centre():
    plain = _points(_compose(SUBLAYER_GBR, _all_on()).svg)
    mirrored = _points(_compose(
        SUBLAYER_GBR,
        PcbRenderParams(paper="white", layers=_all_on().layers, mirror=True),
    ).svg)
    assert sorted(round(210 - x, 3) for x, _ in plain) == sorted(round(x, 3) for x, _ in mirrored)
    assert [round(y, 3) for _, y in plain] == [round(y, 3) for _, y in mirrored]


def test_only_enabled_sublayers_are_drawn_each_in_its_ink():
    params = PcbRenderParams(
        pen="black",
        layers={
            "pads": {"enabled": True, "ink": "blue"},
            "tracks": {"enabled": False},
        },
    )
    res = _compose(SUBLAYER_GBR, params)
    assert [layer.id for layer in res.layers] == ["pads"]
    assert res.layers[0].ink == "blue"
    assert 'data-kp-layer="tracks"' not in res.svg
    assert f'stroke="{INK_HEX["blue"]}"' in res.svg
    assert 'fill="#' not in res.svg  # outline only: no filled pad or pour


def test_svg_has_no_image_and_no_text():
    svg = _compose(SUBLAYER_GBR, _all_on()).svg
    assert "<image" not in svg
    assert "<text" not in svg


def test_white_ink_needs_electric_blue_paper():
    with pytest.raises(ValidationError):
        PcbRenderParams(paper="white", pen="white")
    with pytest.raises(ValidationError):
        PcbRenderParams(paper="electric-blue", pen="red")
    PcbRenderParams(paper="electric-blue", pen="white")
    with pytest.raises(ValidationError):
        PcbRenderParams(paper="white", layers={"pads": {"enabled": True, "ink": "white"}})


def test_a_disabled_layer_may_keep_an_ink_the_paper_does_not_allow():
    PcbRenderParams(paper="white", layers={"pads": {"enabled": False, "ink": "white"}})


def test_extra_fields_are_refused():
    with pytest.raises(ValidationError):
        PcbRenderParams(paper="white", colour="red")


def test_no_enabled_sublayer_is_refused():
    with pytest.raises(PenPlotError) as exc:
        _compose(SUBLAYER_GBR, PcbRenderParams(layers={"pads": {"enabled": False}}))
    assert exc.value.status == 422
    assert "Turn on at least one sub-layer" in exc.value.message


def test_enabled_sublayer_missing_from_the_file_is_a_warning():
    res = _compose(SUBLAYER_GBR, PcbRenderParams(layers={
        "pads": {"enabled": True}, "drills": {"enabled": True},
    }))
    assert res.warnings == ["layer_absent:drills"]


def test_pen_down_ceiling_names_the_cause_and_the_way_out(monkeypatch):
    monkeypatch.setattr(sheet_mod, "PEN_DOWN_LIMIT_MM", 100.0)
    with pytest.raises(PenPlotError) as exc:
        _compose(L_TRACKS, PcbRenderParams(layers={"tracks": {"enabled": True}}))
    assert exc.value.status == 422
    assert "pen-down" in exc.value.message
    assert "Turn off sub-layers" in exc.value.message


def test_plot_time_ceiling_names_the_cause(monkeypatch):
    monkeypatch.setattr(sheet_mod, "PLOT_TIME_LIMIT_S", 0.5)
    with pytest.raises(PenPlotError) as exc:
        _compose(L_TRACKS, PcbRenderParams(layers={"tracks": {"enabled": True}}))
    assert exc.value.status == 422
    assert "min to plot" in exc.value.message


def _post_render(http_client, name: str, payload: bytes, params: object):
    return http_client.post(
        URL,
        files={"file": (name, payload, "application/octet-stream")},
        data={"params": json.dumps(params) if not isinstance(params, str) else params},
    )


_ALL_ON = {
    "paper": "white",
    "pen": "black",
    "layers": {
        "pads": {"enabled": True, "ink": "auto"},
        "tracks": {"enabled": True, "ink": "blue"},
        "pours": {"enabled": True, "ink": "auto"},
        "drills": {"enabled": False, "ink": "auto"},
    },
    "mirror": False,
}


def test_render_over_http_returns_svg_stats_layers_and_token(http_client):
    resp = _post_render(http_client, "board.gbr", SUBLAYER_GBR, _ALL_ON)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "<image" not in body["svg"] and "<text" not in body["svg"]
    assert body["stats"]["strokes"] > 0 and body["stats"]["pen_down_mm"] > 0
    assert body["stats"]["estimated_time_s"] > 0
    assert [layer["id"] for layer in body["layers"]] == ["pads", "tracks", "pours"]
    assert body["design"] is not None
    assert body["design"]["design_id"] == hashlib.sha256(body["svg"].encode("utf-8")).hexdigest()
    assert len(body["design"]["sig"]) == 64
    assert body["svg_url"].endswith(f"/v1/pcb/results/{body['design']['design_id']}")


def test_render_result_route_serves_the_same_svg(http_client):
    body = _post_render(http_client, "board.gbr", SUBLAYER_GBR, _ALL_ON).json()
    got = http_client.get(f"/v1/pcb/results/{body['design']['design_id']}")
    assert got.status_code == 200
    assert got.text == body["svg"]


def test_repeat_render_is_a_cache_hit(http_client):
    params = {**_ALL_ON, "mirror": True}
    _post_render(http_client, "board.gbr", SUBLAYER_GBR, params)
    again = _post_render(http_client, "board.gbr", SUBLAYER_GBR, params).json()
    assert again["cache_hit"] is True


def test_white_on_white_paper_is_422_over_http(http_client):
    params = {**_ALL_ON, "pen": "white"}
    resp = _post_render(http_client, "board.gbr", SUBLAYER_GBR, params)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"
    assert "does not plot on white paper" in resp.json()["error"]["message"]


def test_bad_params_text_is_422_over_http(http_client):
    resp = _post_render(http_client, "board.gbr", SUBLAYER_GBR, "{not json")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


def test_no_enabled_layer_is_422_over_http(http_client):
    params = {**_ALL_ON, "layers": {"pads": {"enabled": False}}}
    resp = _post_render(http_client, "board.gbr", SUBLAYER_GBR, params)
    assert resp.status_code == 422
    assert "Turn on at least one sub-layer" in resp.json()["error"]["message"]
