"""PCB sub-layers: flashes are pads, lines and arcs tracks, regions pours,
Excellon objects drills; one SVG group per sub-layer present."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.pcb import layers
from backend.pcb.layers import Shape, board_shapes, sublayer_groups
from backend.penplot.errors import PenPlotError

FIXTURES = Path(__file__).parent / "fixtures"


def _layer_counts(shapes: list[Shape]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for shape in shapes:
        counts[shape.layer] = counts.get(shape.layer, 0) + 1
    return counts


def test_gerber_objects_split_into_pads_tracks_and_pours():
    shapes = board_shapes((FIXTURES / "pcb_sublayers.gbr").read_bytes())
    # Three flashes (circle, rectangle, obround) are pads; the track and the
    # arc are tracks; the single region is a pour.
    assert _layer_counts(shapes) == {"pads": 3, "tracks": 2, "pours": 1}
    assert all(s.closed for s in shapes if s.layer in ("pads", "pours"))
    assert not any(s.closed for s in shapes if s.layer == "tracks")


def test_arc_is_sampled_along_its_centre_line():
    shapes = board_shapes((FIXTURES / "pcb_sublayers.gbr").read_bytes())
    arc = next(s for s in shapes if s.layer == "tracks" and len(s.points) > 2)
    assert arc.points[0] == pytest.approx((1.0, 4.0))
    assert arc.points[-1] == pytest.approx((2.0, 4.0))
    # Every sampled point lies on the 0.5 mm circle about (1.5, 4.0).
    for x, y in arc.points:
        assert ((x - 1.5) ** 2 + (y - 4.0) ** 2) ** 0.5 == pytest.approx(0.5, abs=1e-6)


def test_excellon_objects_are_drills():
    shapes = board_shapes((FIXTURES / "pcb_minimal.drl").read_bytes())
    assert _layer_counts(shapes) == {"drills": 2}
    assert all(s.closed for s in shapes)


def test_sublayer_groups_emit_one_group_per_present_layer():
    shapes = board_shapes((FIXTURES / "pcb_sublayers.gbr").read_bytes())
    groups = sublayer_groups(shapes)
    assert list(groups) == ["pads", "tracks", "pours"]
    for layer, group in groups.items():
        assert f'id="pcb-{layer}"' in group
        assert f'data-kp-layer="{layer}"' in group
        assert group.endswith("</g>")
    assert "drills" not in groups


def test_sublayer_groups_without_shapes_is_empty():
    assert sublayer_groups([]) == {}


def test_closed_shapes_are_filled_unless_outline_is_requested():
    shapes = board_shapes((FIXTURES / "pcb_sublayers.gbr").read_bytes())
    filled = sublayer_groups(shapes, inks={"pads": "#2456a6"})["pads"]
    assert 'fill="#2456a6"' in filled
    outlined = sublayer_groups(shapes, inks={"pads": "#2456a6"}, outline=True)["pads"]
    assert 'fill="#2456a6"' not in outlined
    assert 'stroke="#2456a6"' in outlined


def test_too_many_objects_is_rejected(monkeypatch):
    monkeypatch.setattr(layers, "MAX_SHAPES", 2)
    with pytest.raises(PenPlotError) as exc:
        board_shapes((FIXTURES / "pcb_sublayers.gbr").read_bytes())
    assert exc.value.status == 422
    assert "too large to plot" in exc.value.message
