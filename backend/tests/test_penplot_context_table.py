"""Gallery context table (issue #1058): schema bounds + verbatim-cell layout."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.penplot import stats_table
from backend.penplot.schemas import ContextTableParams, ContextTableRow

PAGE_W, PAGE_H, MARGIN = 210.0, 297.0, 10.0
ROWS = [("Kerékátmérő", "840 mm"), ("Hajtás", "Hidraulikus")]


def _layout(rows=ROWS, **kw):
    args = dict(position="top-right", page_w=PAGE_W, page_h=PAGE_H,
                margin_mm=MARGIN, display_keys=False)
    args.update(kw)
    return stats_table.layout_stats_table(rows, **args)


def test_empty_rows_is_noop():
    lines, warnings, keepout = _layout([])
    assert lines == [] and warnings == [] and keepout is None


def test_keys_are_not_capitalized_when_display_keys_off():
    assert stats_table._display_key("hajtás") == "Hajtás"
    lines_verbatim, _, _ = _layout([("hajtás", "x")])
    lines_capped, _, _ = _layout([("hajtás", "x")], display_keys=True)
    assert lines_verbatim != lines_capped


def test_layout_returns_cartouche_keepout():
    lines, _, keepout = _layout()
    assert lines and keepout is not None
    x0, y0, x1, y1 = keepout
    assert x1 == pytest.approx(PAGE_W - MARGIN)
    assert y0 == pytest.approx(MARGIN)
    assert y1 > y0


def test_schema_caps_rows_and_cell_length():
    ContextTableParams(enabled=True, rows=[ContextTableRow(label="a", value="b")] * 12)
    with pytest.raises(ValidationError):
        ContextTableParams(rows=[ContextTableRow(label="a", value="b")] * 13)
    with pytest.raises(ValidationError):
        ContextTableRow(label="x" * 65, value="b")
    with pytest.raises(ValidationError):
        ContextTableRow(label="", value="b")
    with pytest.raises(ValidationError):
        ContextTableParams(pad_left_mm=21)
