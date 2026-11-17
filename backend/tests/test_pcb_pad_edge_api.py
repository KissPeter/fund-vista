"""HTTP: a track that runs into a pad is plotted up to the pad edge, not the centre.

The board is mocked as a hand-written top-copper Gerber (no real file needed):
a 2 mm round pad centred at (5, 0) and one track. The track ends on the pad
centre in ``track_to_centre`` and on the pad edge in ``track_to_edge``. Both
boards have the same extent, so the sheet scale is the same and the pen-down
of the trimmed board must equal the control board's pen-down.
"""

from __future__ import annotations

import json

import pytest

URL = "/v1/pcb/render"

_HEADER = (
    b"G04 mocked top copper*\n"
    b"%TF.FileFunction,Copper,L1,Top*%\n"
    b"%FSLAX26Y26*%\n%MOMM*%\n"
    b"%ADD10C,0.250*%\n%ADD11C,2.000*%\n"
    b"G75*\nG01*\n"
)
# Pad: a 2 mm round flash at (5, 0) mm.
_PAD = b"D11*\nX5000000Y0D03*\n"


def _board(track_end_mm: float) -> bytes:
    end = round(track_end_mm * 1e6)
    return (
        _HEADER
        + b"D10*\nX0Y0D02*\n"
        + f"X{end}Y0D01*\n".encode()
        + _PAD
        + b"M02*\n"
    )


TRACK_TO_CENTRE = _board(5.0)  # runs on to the pad centre
TRACK_TO_EDGE = _board(4.0)  # already stops on the pad edge (control)

_PARAMS = {
    "paper": "white",
    "pen": "black",
    "layers": {
        "pads": {"enabled": True, "ink": "auto"},
        "tracks": {"enabled": True, "ink": "auto"},
    },
}


def _render(http_client, payload: bytes, name: str) -> dict:
    resp = http_client.post(
        URL,
        files={"file": (name, payload, "application/octet-stream")},
        data={"params": json.dumps(_PARAMS)},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_track_into_pad_is_plotted_up_to_the_pad_edge(http_client):
    trimmed = _render(http_client, TRACK_TO_CENTRE, "mock-centre.gbr")
    control = _render(http_client, TRACK_TO_EDGE, "mock-edge.gbr")
    assert trimmed["stats"]["pen_down_mm"] == pytest.approx(
        control["stats"]["pen_down_mm"], abs=0.01,
    )
    assert trimmed["stats"]["strokes"] == control["stats"]["strokes"] == 2
