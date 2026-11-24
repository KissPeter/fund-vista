"""Regression: with ``layout`` and ``lines`` absent, the pcb sheet is unchanged.

The fixtures under ``fixtures/pcb_sheet_default_*`` were generated from the
sheet composer BEFORE the page rail (``layout`` / ``lines``) was added. The
render must stay byte for byte equal to them (docs/pcb-page-contract.md D1).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
GBR = (FIXTURES / "pcb_sublayers.gbr").read_bytes()
CASES = ["pcb_sheet_default_pads_tracks", "pcb_sheet_default_all_mirror"]


@pytest.mark.parametrize("name", CASES)
def test_absent_layout_and_lines_render_the_stored_sheet(http_client, name):
    expected = json.loads((FIXTURES / f"{name}.json").read_text())
    resp = http_client.post(
        "/v1/pcb/render",
        files={"file": ("board.gbr", GBR, "application/octet-stream")},
        data={"params": json.dumps(expected["params"])},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["svg"] == (FIXTURES / f"{name}.svg").read_text()
    assert body["stats"] == expected["stats"]
    assert body["rotated"] is expected["rotated"]
