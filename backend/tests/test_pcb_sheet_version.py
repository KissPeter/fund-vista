"""The render cache key carries SHEET_VERSION: changing the plotted geometry must bump it.

A cached sheet is served for 7 days for the same file and params, so a geometry
change (pad-edge trim, drawn pads) without a bump leaves old, wrong sheets live.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from backend.pcb.layers import board_shapes
from backend.pcb.router import SHEET_VERSION
from backend.pcb.schemas import PcbRenderParams
from backend.pcb.sheet import compose_sheet

FIXTURES = Path(__file__).parent / "fixtures"
#: (SHEET_VERSION, sha256 of the ESP32DIV2 sheet). Update BOTH together.
GOLDEN = ("sheet-v2", "a10264317384ef1ace4e1747e27bc536cb911a1fb75a7dd8018473ebae186a3a")


def test_sheet_geometry_change_needs_a_sheet_version_bump():
    shapes = board_shapes((FIXTURES / "ESP32DIV2-pcb.GTL").read_bytes())
    params = PcbRenderParams.model_validate({
        "paper": "white", "pen": "black",
        "layers": {"pads": {"enabled": True, "ink": "blue"},
                   "tracks": {"enabled": True, "ink": "black"}},
    })
    digest = hashlib.sha256(compose_sheet(shapes, params).svg.encode()).hexdigest()
    assert (SHEET_VERSION, digest) == GOLDEN, (
        "The plotted sheet changed: bump SHEET_VERSION in backend/pcb/router.py "
        f"(stale cached sheets) and set GOLDEN to ({SHEET_VERSION!r}, {digest!r})."
    )
