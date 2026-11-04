"""Art placement around the gallery context table (``art_placement.py``).

Mirrors the shop's ``artPlacement.test.ts`` and ``artInk.test.ts`` case for
case: every placement step, each of the four table corners, and the mask
rules (diagonal line, quadrant-only shape, rows from the aspect ratio).
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.penplot import art_placement as ap
from backend.penplot.art_placement import InkMask

COLS = 10
ROWS = 10

CORNERS = ["top-left", "top-right", "bottom-left", "bottom-right"]

AREA = (0.0, 0.0, 200.0, 300.0)
TABLE_W = 60.0
TABLE_H = 30.0
BOX_W = 120.0
BOX_H = 120.0
INSET = 20.0


def mask_with(ink: list[tuple[int, int]]) -> InkMask:
    """10x10 mask with ink only in the listed ``(col, row)`` cells."""
    cells = np.zeros(COLS * ROWS, dtype=bool)
    for c, r in ink:
        cells[r * COLS + c] = True
    return InkMask(cols=COLS, rows=ROWS, cells=cells)


def full_mask() -> InkMask:
    """10x10 mask with every cell inked."""
    return InkMask(cols=COLS, rows=ROWS, cells=np.ones(COLS * ROWS, dtype=bool))


def table_at(corner: str, w: float = TABLE_W, h: float = TABLE_H) -> tuple[float, float, float, float]:
    """Table pinned to a corner of AREA."""
    ax, ay, aw, ah = AREA
    left = corner.endswith("left")
    top = corner.startswith("top")
    x = ax if left else ax + aw - w
    y = ay if top else ay + ah - h
    return (x, y, w, h)


def box_near(corner: str, full_height: bool = False) -> tuple[float, float, float, float]:
    """Box near the table corner with slack on both axes, or a full-height box
    that can only slide sideways."""
    ax, ay, aw, ah = AREA
    left = corner.endswith("left")
    top = corner.startswith("top")
    h = ah if full_height else BOX_H
    inset = 0.0 if full_height else INSET
    x = ax + 40 if left else ax + aw - 40 - BOX_W
    y = ay + inset if top else ay + ah - inset - h
    return (x, y, BOX_W, h)


def near_cell(corner: str) -> tuple[int, int]:
    """Cell of a 10x10 mask nearest to the table corner."""
    return (0 if corner.endswith("left") else COLS - 1, 0 if corner.startswith("top") else ROWS - 1)


def far_cell(corner: str) -> tuple[int, int]:
    """Cell of a 10x10 mask farthest from the table corner."""
    return (COLS - 1 if corner.endswith("left") else 0, ROWS - 1 if corner.startswith("top") else 0)


def assert_inside_area(box: tuple[float, float, float, float]) -> None:
    eps = 1e-9
    x, y, w, h = box
    assert x >= AREA[0] - eps
    assert y >= AREA[1] - eps
    assert x + w <= AREA[0] + AREA[2] + eps
    assert y + h <= AREA[1] + AREA[3] + eps


# -- inkOverlapsRect ---------------------------------------------------------------

BOX = (0.0, 0.0, 100.0, 100.0)
RECT = (50.0, 50.0, 10.0, 10.0)


def test_overlap_false_when_the_only_ink_is_far():
    assert ap.ink_overlaps_rect(mask_with([(0, 0)]), BOX, RECT, 2.0) is False


def test_overlap_true_when_an_ink_cell_sits_under_the_rect():
    assert ap.ink_overlaps_rect(mask_with([(5, 5)]), BOX, RECT, 0.0) is True


def test_overlap_counts_the_gap():
    # Cell (4, 5) covers x [40, 50): it touches the rect edge at x = 50.
    ink = mask_with([(4, 5)])
    assert ap.ink_overlaps_rect(ink, BOX, RECT, 0.0) is False
    assert ap.ink_overlaps_rect(ink, BOX, RECT, 2.0) is True


def test_overlap_null_mask_is_the_whole_box():
    assert ap.ink_overlaps_rect(None, BOX, RECT, 0.0) is True
    assert ap.ink_overlaps_rect(None, BOX, (200.0, 200.0, 10.0, 10.0), 2.0) is False


# -- placeArtAroundTable, every step x every corner ---------------------------------


@pytest.mark.parametrize("corner", CORNERS)
def test_returns_box_unchanged_when_ink_does_not_reach_the_table(corner):
    box = box_near(corner)
    result = ap.place_art_around_table(AREA, box, mask_with([far_cell(corner)]), table_at(corner))
    assert result == ap.ArtPlacement(box, 0.0, 0.0, 1.0, False)


@pytest.mark.parametrize("corner", CORNERS)
def test_slides_along_the_axis_with_slack_smallest_shift_wins(corner):
    # Clearing needs 12 mm vertically (2 mm gap included) versus 22 mm
    # horizontally, so the vertical slide wins. The away direction is toward
    # the area centre.
    box = box_near(corner)
    result = ap.place_art_around_table(AREA, box, mask_with([near_cell(corner)]), table_at(corner))
    dy = 12.0 if corner.startswith("top") else -12.0
    assert result.overlap is False
    assert result.scale == 1
    assert result.dx == 0
    assert result.dy == dy
    assert result.box == pytest.approx((box[0], box[1] + dy, box[2], box[3]))
    assert_inside_area(result.box)


@pytest.mark.parametrize("corner", CORNERS)
def test_slides_sideways_when_only_the_horizontal_axis_has_slack(corner):
    # Full-height box: no vertical slack, so the 22 mm horizontal slide is the
    # only option.
    box = box_near(corner, full_height=True)
    result = ap.place_art_around_table(AREA, box, mask_with([near_cell(corner)]), table_at(corner))
    dx = 22.0 if corner.endswith("left") else -22.0
    assert result.overlap is False
    assert result.scale == 1
    assert result.dy == 0
    assert result.dx == dx
    assert result.box == pytest.approx((box[0] + dx, box[1], box[2], box[3]))
    assert_inside_area(result.box)


@pytest.mark.parametrize("corner", CORNERS)
def test_shrinks_toward_the_opposite_corner_when_there_is_no_slack(corner):
    # Box equals the area, so no slide is possible. Full ink: the shrunk box
    # clears the table first at scale 0.88 (0.90 still touches it).
    box = AREA
    result = ap.place_art_around_table(AREA, box, full_mask(), table_at(corner))
    left = corner.endswith("left")
    top = corner.startswith("top")
    w = 200 * 0.88
    h = 300 * 0.88
    x = AREA[0] + AREA[2] - w if left else AREA[0]
    y = AREA[1] + AREA[3] - h if top else AREA[1]
    assert result.overlap is False
    assert result.scale < 1
    assert result.scale >= 0.8
    assert result.scale == pytest.approx(0.88, abs=1e-6)
    assert result.box == pytest.approx((x, y, w, h), abs=1e-6)
    assert result.dx == pytest.approx(x - box[0], abs=1e-6)
    assert result.dy == pytest.approx(y - box[1], abs=1e-6)
    assert_inside_area(result.box)


@pytest.mark.parametrize("corner", CORNERS)
def test_returns_original_box_with_overlap_when_nothing_clears(corner):
    # The table covers most of the area: even the 0.8 shrink still touches it.
    big_table = table_at(corner, 160.0, 250.0)
    box = AREA
    result = ap.place_art_around_table(AREA, box, full_mask(), big_table)
    assert result == ap.ArtPlacement(box, 0.0, 0.0, 1.0, True)
    assert_inside_area(result.box)


def test_no_table_returns_the_box_unchanged():
    box = box_near("top-left")
    result = ap.place_art_around_table(AREA, box, full_mask(), None)
    assert result == ap.ArtPlacement(box, 0.0, 0.0, 1.0, False)


def test_null_mask_is_full_ink_and_still_avoids_the_table():
    # Without a mask the whole box is ink, so it must slide clear of the table:
    # 12 mm down (the vertical clearance) beats 22 mm sideways.
    box = box_near("top-left")
    result = ap.place_art_around_table(AREA, box, None, table_at("top-left"))
    assert result.overlap is False
    assert result.dx == 0
    assert result.dy == 12.0
    assert result.box == pytest.approx((box[0], box[1] + 12.0, box[2], box[3]))
    assert_inside_area(result.box)


# -- ink_mask ------------------------------------------------------------------------


def _cells(mask: InkMask) -> np.ndarray:
    return mask.cells.reshape(mask.rows, mask.cols)


def test_diagonal_line_inks_its_diagonal_and_leaves_the_opposite_corners_empty():
    mask = ap.ink_mask([[(0.0, 0.0), (100.0, 100.0)]], 100.0, 100.0, cols=64)
    assert mask is not None
    grid = _cells(mask)
    assert all(grid[k, k] for k in range(64))
    assert not grid[0, 63]
    assert not grid[63, 0]


def test_quadrant_shape_leaves_the_opposite_quadrant_empty():
    square = [(5.0, 5.0), (35.0, 5.0), (35.0, 35.0), (5.0, 35.0), (5.0, 5.0)]
    mask = ap.ink_mask([square], 100.0, 100.0, cols=64)
    assert mask is not None
    grid = _cells(mask)
    assert grid[3, 10]
    assert not grid[32:, 32:].any()


def test_rows_follow_the_viewport_aspect_ratio():
    mask = ap.ink_mask([[(0.0, 0.0), (10.0, 0.0)]], 200.0, 100.0, cols=64)
    assert mask is not None
    assert mask.cols == 64
    assert mask.rows == 32
    assert mask.cells.shape == (64 * 32,)


def test_one_point_polyline_inks_a_dilated_cell():
    mask = ap.ink_mask([[(50.0, 50.0)]], 100.0, 100.0, cols=10)
    assert mask is not None
    grid = _cells(mask)
    assert grid[4:7, 4:7].all()
    assert not grid[0, 0]


def test_segments_are_not_joined_across_polylines():
    # Two separate strokes: the gap between their end and start stays empty.
    mask = ap.ink_mask([[(0.0, 0.0), (10.0, 0.0)], [(90.0, 90.0), (100.0, 90.0)]], 100.0, 100.0, cols=10)
    assert mask is not None
    grid = _cells(mask)
    assert not grid[5, 5]


def test_empty_polylines_give_an_empty_mask():
    mask = ap.ink_mask([], 100.0, 100.0, cols=10)
    assert mask is not None
    assert not mask.cells.any()


def test_degenerate_viewport_gives_no_mask():
    assert ap.ink_mask([[(0.0, 0.0), (1.0, 1.0)]], 0.0, 100.0) is None
    assert ap.ink_mask([[(0.0, 0.0), (1.0, 1.0)]], 100.0, -1.0) is None


# -- mask frame: the mask maps onto the content rect ------------------------------


def test_content_rect_fits_and_centres_the_viewport_in_the_box():
    box = (0.0, 0.0, 100.0, 100.0)
    assert ap.content_rect(box, 2.0) == (0.0, 25.0, 100.0, 50.0)
    assert ap.content_rect(box, 0.5) == (25.0, 0.0, 50.0, 100.0)
    assert ap.content_rect(box, 1.0) == box


def test_mask_maps_onto_the_content_rect_not_the_whole_box():
    # A wide viewport (aspect 2) in a 100 x 100 box is drawn at y 25..75. The
    # mask's top-left cell is therefore at y 25..35, not at the box top.
    cells = np.zeros(5 * 10, dtype=bool)
    cells[0] = True
    box = (0.0, 0.0, 100.0, 100.0)
    wide = InkMask(cols=10, rows=5, cells=cells, aspect=2.0)
    assert ap.ink_overlaps_rect(wide, box, (0.0, 0.0, 10.0, 10.0), 0.0) is False
    assert ap.ink_overlaps_rect(wide, box, (0.0, 25.0, 10.0, 10.0), 0.0) is True
    # Without an aspect the mask fills the box, so the same cell is at the top.
    filled = InkMask(cols=10, rows=5, cells=cells)
    assert ap.ink_overlaps_rect(filled, box, (0.0, 0.0, 10.0, 10.0), 0.0) is True


def test_ink_mask_records_the_viewport_aspect():
    assert ap.ink_mask([[(0.0, 0.0), (10.0, 0.0)]], 200.0, 100.0, cols=10).aspect == 2.0
    assert ap.ink_mask([], 300.0, 100.0, cols=10).aspect == 3.0


# -- non-finite single points -------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_one_point_polyline_is_dropped(bad):
    # Only the finite (5, 5) point inks, as one cell dilated to 3 x 3.
    mask = ap.ink_mask([[(bad, 1.0)], [(5.0, 5.0)]], 10.0, 10.0, cols=10)
    assert int(mask.cells.sum()) == 9


def test_ink_on_the_right_and_bottom_viewport_edge_is_kept():
    # A full-bleed edge sits exactly on the far side of the grid (x = cols,
    # y = rows); it must still ink the last column and row.
    right = ap.ink_mask([[(100.0, 0.0), (100.0, 100.0)]], 100.0, 100.0, cols=10)
    assert right.cells.reshape(right.rows, right.cols)[:, 9].all()
    bottom = ap.ink_mask([[(0.0, 100.0), (100.0, 100.0)]], 100.0, 100.0, cols=10)
    assert bottom.cells.reshape(bottom.rows, bottom.cols)[9, :].all()


# -- long strokes: clipped to the grid before sampling -----------------------------


def test_far_line_masks_the_same_as_the_finite_line():
    # The stroke runs far outside the viewport. Clipping to the grid first makes
    # the in-grid sampling identical to the finite line's, so the masks match.
    # (Before the clip, the far line's sampling was capped and inked 12 cells
    # where the finite line inked 192.)
    finite = ap.ink_mask([[(-1000.0, 10.0), (1000.0, 10.0)]], 100.0, 100.0, cols=64)
    far = ap.ink_mask([[(-1000000.0, 10.0), (1000000.0, 10.0)]], 100.0, 100.0, cols=64)
    assert finite is not None and far is not None
    assert far.cells.sum() > 0
    assert np.array_equal(far.cells, finite.cells)


def test_diagonal_far_line_masks_the_same_as_the_finite_line():
    finite = ap.ink_mask([[(-500.0, -500.0), (500.0, 500.0)]], 100.0, 100.0, cols=64)
    far = ap.ink_mask([[(-1000000.0, -1000000.0), (1000000.0, 1000000.0)]], 100.0, 100.0, cols=64)
    assert finite is not None and far is not None
    assert np.array_equal(far.cells, finite.cells)


def test_segment_wholly_off_the_viewport_marks_nothing():
    # Off the left, off the top and off the right of a 100 x 100 viewport. The
    # segments are not clamped into the edge cells any more.
    off_left = ap.ink_mask([[(-100.0, 50.0), (-10.0, 50.0)]], 100.0, 100.0, cols=10)
    off_top = ap.ink_mask([[(50.0, -100.0), (50.0, -10.0)]], 100.0, 100.0, cols=10)
    off_right = ap.ink_mask([[(150.0, 0.0), (150.0, 100.0)]], 100.0, 100.0, cols=10)
    for mask in (off_left, off_top, off_right):
        assert mask is not None
        assert not mask.cells.any()


def test_segment_crossing_the_viewport_inks_its_in_grid_part():
    # Crosses a 10 x 10 grid at y = 5 cells from far outside on the left. The
    # line inks row 5, dilated to rows 4..6 across the whole grid width.
    mask = ap.ink_mask([[(-1000.0, 50.0), (1000.0, 50.0)]], 100.0, 100.0, cols=10)
    assert mask is not None
    grid = _cells(mask)
    assert grid[4:7, :].all()
    assert not grid[:4].any()
    assert not grid[7:].any()
