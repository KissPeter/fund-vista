"""Stats-table cartouche: artwork under the table + its insets is knocked out."""

from __future__ import annotations

import pytest

from backend.penplot import stats_table
from backend.penplot.knockout import knock_out

RECT = (10.0, 10.0, 50.0, 40.0)


def _inside(pt, rect=RECT, eps=1e-6):
    return rect[0] + eps < pt[0] < rect[2] - eps and rect[1] + eps < pt[1] < rect[3] - eps


def test_stroke_fully_outside_is_untouched():
    line = [(0.0, 0.0), (5.0, 5.0), (5.0, 60.0)]
    assert knock_out([line], RECT) == [line]


def test_stroke_fully_inside_is_removed():
    assert knock_out([[(20.0, 20.0), (30.0, 30.0)]], RECT) == []


def test_crossing_stroke_is_split_at_the_edge():
    out = knock_out([[(0.0, 20.0), (60.0, 20.0)]], RECT)
    assert out == [[(0.0, 20.0), (10.0, 20.0)], [(50.0, 20.0), (60.0, 20.0)]]


def test_stroke_entering_and_staying_inside_is_clipped_to_the_edge():
    out = knock_out([[(0.0, 20.0), (30.0, 20.0)]], RECT)
    assert out == [[(0.0, 20.0), (10.0, 20.0)]]


def test_polyline_with_outside_runs_keeps_connected_pieces():
    line = [(0.0, 5.0), (60.0, 5.0), (60.0, 60.0)]  # runs above the rect
    assert knock_out([line], RECT) == [line]


def test_nothing_survives_strictly_inside():
    lines = [[(x, y), (x + 70.0, y + 3.0)] for x, y in ((0, 5), (0, 15), (0, 25), (0, 35))]
    for pl in knock_out(lines, RECT):
        assert not any(_inside(p) for p in pl)
        # midpoints of each kept segment too (a long segment could span the box)
        for a, b in zip(pl, pl[1:]):
            assert not _inside(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))


def test_degenerate_input_is_dropped():
    assert knock_out([[(1.0, 1.0)], []], RECT) == []


PAGE_W, PAGE_H, MARGIN = 210.0, 297.0, 10.0
ROWS = [("highways", 1026), ("roads", 16404)]


def _layout(**kw):
    args = dict(position="top-right", page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN)
    args.update(kw)
    return stats_table.layout_stats_table(ROWS, **args)


def test_empty_rows_have_no_keepout():
    assert stats_table.layout_stats_table([], position="top-left", page_w=PAGE_W,
                                          page_h=PAGE_H, margin_mm=MARGIN)[2] is None


@pytest.mark.parametrize("position", ["top-left", "top-right", "bottom-left", "bottom-right"])
def test_keepout_covers_table_and_reaches_the_margin_corner(position):
    lines, _, keep = _layout(position=position, pad_left_mm=10, pad_right_mm=10,
                             pad_top_mm=10, pad_bottom_mm=10)
    xs = [x for pl in lines for x, _ in pl]
    ys = [y for pl in lines for _, y in pl]
    # Every table stroke sits inside the keep-out.
    assert keep[0] <= min(xs) and max(xs) <= keep[2]
    assert keep[1] <= min(ys) and max(ys) <= keep[3]
    # The keep-out extends to the margin on the anchored corner (inset strip included).
    if position.endswith("left"):
        assert keep[0] == MARGIN
    else:
        assert keep[2] == PAGE_W - MARGIN
    if position.startswith("top"):
        assert keep[1] == MARGIN
    else:
        assert keep[3] == PAGE_H - MARGIN


def test_bottom_keepout_stops_above_the_label_strip():
    _, _, keep = _layout(position="bottom-left", reserve_bottom_mm=12.0)
    assert keep[3] == PAGE_H - MARGIN - 12.0


def test_render_wrapper_matches_layout_lines():
    lines, warnings = stats_table.render_stats_table(
        ROWS, position="top-right", page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN)
    l2, w2, _ = _layout()
    assert (lines, warnings) == (l2, w2)
