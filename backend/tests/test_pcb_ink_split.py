"""Ink split for kind ``pcb``: one SVG per distinct sub-layer ink.

The unit cases split an in-process sheet. The HTTP cases render through
``POST /v1/pcb/render`` and then ask ``POST /v1/fulfill/ink-split`` for the
passes, against the live server that shares the test HMAC secret.
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.fulfillment import ink_split
from backend.fulfillment.ink_split import INK_HEX, pcb_group_ink, split_by_ink
from backend.pcb.layers import board_shapes
from backend.pcb.schemas import PcbRenderParams
from backend.pcb.sheet import compose_sheet
from backend.penplot import tokens as design_tokens
from backend.tests.helpers import TEST_HMAC_SECRET

GBR = (Path(__file__).parent / "fixtures" / "pcb_sublayers.gbr").read_bytes()
SPLIT_URL = "/v1/fulfill/ink-split"


def _params(**overrides) -> dict:
    base = {
        "paper": "white",
        "pen": "black",
        "layers": {
            "pads": {"enabled": True, "ink": "auto"},
            "tracks": {"enabled": True, "ink": "blue"},
            "pours": {"enabled": False, "ink": "auto"},
            "drills": {"enabled": False, "ink": "auto"},
        },
        "mirror": False,
    }
    base.update(overrides)
    return base


def test_group_mapping_rides_the_resolved_layer_ink_or_the_pen():
    layer_inks = {"tracks": "blue"}
    assert pcb_group_ink("pcb-tracks", "black", layer_inks) == "blue"
    assert pcb_group_ink("pcb-pads", "black", layer_inks) == "black"
    assert pcb_group_ink("", "red", layer_inks) == "red"


def test_in_process_sheet_splits_into_one_file_per_ink():
    params = PcbRenderParams(**_params())
    svg = compose_sheet(board_shapes(GBR), params).svg
    files = split_by_ink(svg, lambda gid: pcb_group_ink(gid, params.pen, params.enabled_layers()))
    assert [f.ink for f in files] == ["black", "blue"]
    assert files[0].layers == ["pcb-pads"]
    assert files[1].layers == ["pcb-tracks"]
    assert files[1].hex == INK_HEX["blue"]
    assert 'stroke="#2456a6"' in files[1].svg
    assert "pcb-pads" not in files[1].svg


def _render(http_client, params: dict) -> dict:
    resp = http_client.post(
        "/v1/pcb/render",
        files={"file": ("board.gbr", GBR, "application/octet-stream")},
        data={"params": json.dumps(params)},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _split(http_client, design: dict, params: dict):
    return http_client.post(SPLIT_URL, json={
        "design": {"design_id": design["design_id"], "exp": design["exp"], "sig": design["sig"]},
        "kind": "pcb",
        "params": params,
    })


def test_pcb_ink_split_over_http_serves_one_file_per_ink(http_client):
    params = _params()
    rendered = _render(http_client, params)
    resp = _split(http_client, rendered["design"], params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "pcb"
    assert body["design_id"] == rendered["design"]["design_id"]
    assert [f["ink"] for f in body["files"]] == ["black", "blue"]
    assert [f["layers"] for f in body["files"]] == [["pcb-pads"], ["pcb-tracks"]]
    assert body["files"][1]["hex"] == INK_HEX["blue"]


def test_pcb_ink_split_merges_layers_that_share_the_pen(http_client):
    params = _params(layers={
        "pads": {"enabled": True, "ink": "auto"},
        "tracks": {"enabled": True, "ink": "auto"},
    })
    rendered = _render(http_client, params)
    body = _split(http_client, rendered["design"], params).json()
    assert [f["ink"] for f in body["files"]] == ["black"]
    assert body["files"][0]["layers"] == ["pcb-pads", "pcb-tracks"]


def test_pcb_ink_split_refuses_params_that_differ_from_the_render(http_client):
    params = _params()
    rendered = _render(http_client, params)
    resp = _split(http_client, rendered["design"], _params(pen="red"))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "design_mismatch"


def test_pcb_ink_split_refuses_a_draft_that_is_no_longer_cached(http_client):
    design_id = "ab" * 32
    exp = 4102444800  # 2100-01-01: the token is valid, the draft is not
    sig = design_tokens.sign_design_token(design_id=design_id, exp=exp, secret=TEST_HMAC_SECRET)
    resp = http_client.post(SPLIT_URL, json={
        "design": {"design_id": design_id, "exp": exp, "sig": sig},
        "kind": "pcb",
        "params": _params(),
    })
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "draft_expired"


def test_pcb_ink_split_refuses_inks_and_convert(http_client):
    params = _params()
    rendered = _render(http_client, params)
    design = rendered["design"]
    with_inks = http_client.post(SPLIT_URL, json={
        "design": design, "kind": "pcb", "params": params,
        "inks": {"pen": "black"},
    })
    assert with_inks.status_code == 422
    with_convert = http_client.post(SPLIT_URL, json={
        "design": design, "kind": "pcb", "params": params,
        "convert": {},
    })
    assert with_convert.status_code == 422


def test_pcb_ink_split_still_requires_inks_for_airport(http_client):
    resp = http_client.post(SPLIT_URL, json={
        "design": {"design_id": "ab" * 32, "exp": 4102444800,
                   "sig": design_tokens.sign_design_token(
                       design_id="ab" * 32, exp=4102444800, secret=TEST_HMAC_SECRET)},
        "kind": "airport",
        "params": {"icao": "LHBP"},
    })
    assert resp.status_code == 422


def test_ink_split_module_exposes_the_pcb_prefix():
    assert ink_split.PCB_GROUP_PREFIX == "pcb-"
