"""Tracks stop at the edge of the pad they run into (no stub drawn inside a pad ring)."""

from __future__ import annotations

import pytest

from backend.pcb.layers import Shape
from backend.pcb.pad_edge import trim_tracks_at_pads
from backend.pcb.schemas import PcbRenderParams
from backend.pcb.sheet import compose_sheet

# A 2 mm square pad centred on the origin: outline from -1 to 1 on both axes.
PAD = Shape(layer="pads", points=((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)), closed=True)


def _track(*points: tuple[float, float]) -> Shape:
    return Shape(layer="tracks", points=tuple(points), closed=False)


def _tracks(shapes: list[Shape]) -> list[list[tuple[float, float]]]:
    return [list(s.points) for s in shapes if s.layer == "tracks"]


def test_track_ending_on_pad_centre_stops_at_the_pad_edge():
    out = trim_tracks_at_pads([PAD, _track((-5.0, 0.0), (0.0, 0.0))])
    (piece,) = _tracks(out)
    assert piece[0] == (-5.0, 0.0)
    assert piece[-1] == pytest.approx((-1.0, 0.0))


def test_track_crossing_a_pad_is_split_around_it():
    out = trim_tracks_at_pads([PAD, _track((-5.0, 0.0), (5.0, 0.0))])
    left, right = _tracks(out)
    assert left[-1] == pytest.approx((-1.0, 0.0))
    assert right[0] == pytest.approx((1.0, 0.0))
    assert right[-1] == (5.0, 0.0)


def test_track_inside_a_pad_vanishes():
    out = trim_tracks_at_pads([PAD, _track((-0.5, 0.0), (0.5, 0.0))])
    assert _tracks(out) == []
    assert [s.layer for s in out] == ["pads"]


def test_track_touching_no_pad_is_unchanged():
    track = _track((3.0, 3.0), (6.0, 3.0), (6.0, 6.0))
    out = trim_tracks_at_pads([PAD, track])
    assert _tracks(out) == [[(3.0, 3.0), (6.0, 3.0), (6.0, 6.0)]]


def test_bent_track_is_cut_on_the_segment_that_enters_the_pad():
    out = trim_tracks_at_pads([PAD, _track((-5.0, -5.0), (-5.0, 0.0), (0.0, 0.0))])
    (piece,) = _tracks(out)
    assert piece[0] == (-5.0, -5.0)
    assert piece[1] == (-5.0, 0.0)
    assert piece[-1] == pytest.approx((-1.0, 0.0))
    assert len(piece) == 3


def test_no_pads_means_no_change():
    track = _track((-5.0, 0.0), (0.0, 0.0))
    assert trim_tracks_at_pads([track]) == [track]


def test_compose_sheet_plots_the_trimmed_track_when_pads_are_plotted():
    shapes = [PAD, _track((-5.0, 0.0), (0.0, 0.0))]
    result = compose_sheet(
        shapes, PcbRenderParams(layers={"pads": {"enabled": True}, "tracks": {"enabled": True}}),
    )
    tracks = next(layer for layer in result.layers if layer.id == "tracks")
    # The track is one piece, the pad outline is the other path.
    assert tracks.paths == 1
    assert result.stats.strokes == 2
