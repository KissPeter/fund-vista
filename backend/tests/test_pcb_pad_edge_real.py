"""Tracks stop at pad edges on a real board (ESP32DIV2 top copper, PCBWay/Altium export)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.pcb.layers import board_shapes
from backend.pcb.pad_edge import _inside, trim_tracks_at_pads

FIXTURE = Path(__file__).parent / "fixtures" / "ESP32DIV2-pcb.GTL"
URL = "/v1/pcb/render"
_PARAMS = {
    "paper": "white",
    "pen": "black",
    "layers": {
        "pads": {"enabled": True, "ink": "auto"},
        "tracks": {"enabled": True, "ink": "auto"},
    },
}


@pytest.fixture(scope="module")
def board():
    return board_shapes(FIXTURE.read_bytes())


def _open_length(shapes) -> float:
    total = 0.0
    for shape in shapes:
        if shape.layer == "tracks" and not shape.closed:
            total += sum(
                ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
                for (x0, y0), (x1, y1) in zip(shape.points, shape.points[1:])
            )
    return total


def test_board_parses_into_pads_and_tracks(board):
    layers = {layer: sum(1 for s in board if s.layer == layer) for layer in ("pads", "tracks")}
    assert layers["pads"] > 100
    assert layers["tracks"] > 100


def test_no_track_runs_into_a_pad_after_trimming(board):
    pads = [list(s.points) for s in board if s.layer == "pads" and s.closed]
    trimmed = trim_tracks_at_pads(board)
    offenders = 0
    for shape in trimmed:
        if shape.layer != "tracks" or shape.closed:
            continue
        for (x0, y0), (x1, y1) in zip(shape.points, shape.points[1:]):
            mid = ((x0 + x1) / 2, (y0 + y1) / 2)
            if any(_inside(mid, pad) for pad in pads):
                offenders += 1
    assert offenders == 0


def test_trimming_shortens_the_tracks_on_the_real_board(board):
    before = _open_length(board)
    after = _open_length(trim_tracks_at_pads(board))
    assert after < before
    assert before - after > 0.1  # mm: the board has tracks that run into pads


def test_render_api_accepts_the_real_board(http_client):
    resp = http_client.post(
        URL,
        files={"file": ("ESP32DIV2-pcb.GTL", FIXTURE.read_bytes(), "application/octet-stream")},
        data={"params": json.dumps(_PARAMS)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [layer["id"] for layer in body["layers"]] == ["pads", "tracks"]
    assert body["stats"]["pen_down_mm"] > 0
    assert body["stats"]["strokes"] > 0
