"""PCB sniff: an Excellon header wins over Gerber markers (routed slots use G01)."""

from __future__ import annotations

from pathlib import Path

from backend.pcb.layers import board_shapes
from backend.pcb.render import _sniff

FIXTURES = Path(__file__).parent / "fixtures"

# Route-mode Excellon: G00 move, M15 pen down, G01 routed slot, M16 pen up.
ROUTED_SLOT = (
    b"M48\nMETRIC,TZ\nT01C0.800\n%\nT01\n"
    b"G00X1.000Y1.000\nM15\nG01X5.000Y2.000\nM16\nM30\n"
)
# The reviewer's drill-mode file: starts with M48, has G05 and G01.
DRILL_MODE_WITH_G01 = (
    b"M48\nFMAT,2\nMETRIC,TZ\nT01C0.8\n%\nT01\nX1.0Y1.0\nG05\nG01X5.0Y5.0\nM30\n"
)


def test_routed_excellon_is_sniffed_as_excellon():
    assert _sniff(ROUTED_SLOT) == "excellon"
    assert _sniff(DRILL_MODE_WITH_G01) == "excellon"


def test_routed_excellon_keeps_its_slot_as_a_drill_stroke():
    shapes = board_shapes(ROUTED_SLOT)
    assert [(s.layer, s.closed) for s in shapes] == [("drills", False)]
    assert shapes[0].points[0] != shapes[0].points[-1]


def test_gerber_with_g01_is_still_gerber():
    assert _sniff((FIXTURES / "pcb_sublayers.gbr").read_bytes()) == "gerber"
