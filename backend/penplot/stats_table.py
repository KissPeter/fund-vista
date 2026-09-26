"""Layer-stats overlay table, drawn as plotter strokes in a page corner.

The citymap/airport designers show per-layer ``path_counts`` (e.g.
``{"highways": 1026, "roads": 16404}``); this module plots them as a
bordered two-column table (capitalized key | value) anchored inside the
page margins at one of the four corners. Same conventions as the
title-block label (``labels.py``): single-stroke Hershey ``futural``
glyphs laid out directly in page-millimetre space, appended to the laid-out
geometry *before* quantize so the table snaps to the 0.02 mm grid, joins
travel sorting and is counted in stats like any other stroke.

An empty row list is a silent no-op so the frontend can leave the toggle
on before the first import lands. Bottom-anchored tables sit above the
label strip (``reserve_bottom_mm``) instead of sliding under it.
"""

from __future__ import annotations

from backend.penplot.hershey_fonts import FACES
from backend.penplot.methods import Polyline

WARNING_UNSUPPORTED = "stats_table_unsupported_characters"

FONT = "futural"
#: Glyph height of the table cells.
HEIGHT_MM = 3.0
#: Horizontal breathing room between cell text and the column rules.
CELL_PAD_X_MM = 1.5
#: Vertical breathing room between cell text and the row rules.
CELL_PAD_Y_MM = 1.0
#: Gap kept from neighbouring strips (label divider / page edge rules).
TABLE_GAP_MM = 1.0
#: Hard cap so a hostile row list cannot blow up the geometry stage.
MAX_ROWS = 20


def _display_key(key: str) -> str:
    """Raw layer id (``highways``) -> drawn cell text (``Highways``)."""
    return key[:1].upper() + key[1:] if key else key


def _text_lines(text: str, height_mm: float) -> tuple[list[Polyline], float, list[str]]:
    """One line of Hershey text in local coords (x from 0, y-down from 0).

    Returns (polylines, advance_width, warnings); unsupported characters
    are skipped and reported once via ``stats_table_unsupported_characters``.
    """
    face = FACES[FONT]
    cap_height, space_advance, glyphs = (
        face["cap_height"], face["space_advance"], face["glyphs"],
    )
    s = max(height_mm, 1e-9) / cap_height
    lines: list[Polyline] = []
    missing: list[str] = []
    cursor = 0.0
    for ch in text:
        if ch == " ":
            cursor += space_advance * s
            continue
        glyph = glyphs.get(ch)
        if glyph is None:
            if ch not in missing:
                missing.append(ch)
            continue
        for stroke in glyph["lines"]:
            lines.append([(cursor + x * s, y * s) for x, y in stroke])
        cursor += glyph["advance"] * s
    warnings: list[str] = []
    if missing:
        warnings.append(f"{WARNING_UNSUPPORTED}:{''.join(sorted(missing))[:20]}")
    return lines, cursor, warnings


def render_stats_table(
    rows: list[tuple[str, int]],
    *,
    position: str,
    page_w: float,
    page_h: float,
    margin_mm: float,
    reserve_bottom_mm: float = 0.0,
) -> tuple[list[Polyline], list[str]]:
    """Lay out the stats table in mm space. See module docstring.

    ``rows`` are (raw layer key, path count) pairs — already filtered to
    non-zero layers by the caller. ``position`` is one of ``top-left``,
    ``top-right``, ``bottom-left``, ``bottom-right`` (anything else falls
    back to ``top-right``).
    """
    warnings: list[str] = []
    cells = [( _display_key(key), str(value)) for key, value in rows[:MAX_ROWS]]
    if not cells:
        return [], warnings

    key_widths: list[float] = []
    val_widths: list[float] = []
    text_blocks: list[tuple[list[Polyline], list[Polyline]]] = []
    text_h = 0.0
    for key_text, val_text in cells:
        key_lines, key_w, key_warn = _text_lines(key_text, HEIGHT_MM)
        val_lines, val_w, val_warn = _text_lines(val_text, HEIGHT_MM)
        warnings.extend(key_warn)
        warnings.extend(val_warn)
        if key_lines:
            ys = [y for pl in key_lines for _, y in pl]
            text_h = max(text_h, max(ys) - min(ys))
        text_blocks.append((key_lines, val_lines))
        key_widths.append(key_w)
        val_widths.append(val_w)
    if not any(key_widths) and not any(val_widths):
        return [], warnings
    if text_h <= 1e-9:
        text_h = HEIGHT_MM

    col0 = max(key_widths) + 2 * CELL_PAD_X_MM
    col1 = max(val_widths) + 2 * CELL_PAD_X_MM
    row_h = text_h + 2 * CELL_PAD_Y_MM
    table_w = col0 + col1
    table_h = row_h * len(cells)

    inner_left = margin_mm + TABLE_GAP_MM
    inner_right = page_w - margin_mm - TABLE_GAP_MM
    inner_top = margin_mm + TABLE_GAP_MM
    inner_bottom = page_h - margin_mm - TABLE_GAP_MM - max(reserve_bottom_mm, 0.0)
    inner_w = max(inner_right - inner_left, 1e-9)
    inner_h = max(inner_bottom - inner_top, 1e-9)

    # Oversized tables scale down uniformly to fit the inner rect.
    fit = min(1.0, inner_w / table_w, inner_h / table_h)

    pos = position if position in (
        "top-left", "top-right", "bottom-left", "bottom-right",
    ) else "top-right"
    x0 = inner_left if pos.endswith("left") else inner_right - table_w * fit
    y0 = inner_top if pos.startswith("top") else inner_bottom - table_h * fit

    def _map(x: float, y: float) -> tuple[float, float]:
        return (x0 + x * fit, y0 + y * fit)

    lines: list[Polyline] = []
    # Border: closed sharp-corner rect (a table frame, not a rounded page frame).
    x1, y1 = table_w, table_h
    lines.append([_map(x0_, y0_) for x0_, y0_ in
                  [(0.0, 0.0), (x1, 0.0), (x1, y1), (0.0, y1), (0.0, 0.0)]])
    # Column divider between key and value columns.
    lines.append([_map(col0, 0.0), _map(col0, table_h)])
    # Row dividers between cells.
    for i in range(1, len(cells)):
        y = i * row_h
        lines.append([_map(0.0, y), _map(table_w, y)])
    # Cell text: left-aligned in each column, vertically centred in the row.
    for i, (key_lines, val_lines) in enumerate(text_blocks):
        top = i * row_h + CELL_PAD_Y_MM
        for block, col_x in ((key_lines, CELL_PAD_X_MM), (val_lines, col0 + CELL_PAD_X_MM)):
            if not block:
                continue
            ys = [y for pl in block for _, y in pl]
            dy = top + (text_h - (max(ys) - min(ys))) / 2.0 - min(ys)
            lines.extend([[(_map(x + col_x, y + dy)) for x, y in pl] for pl in block])
    # Deduplicate warnings (one row per layer keeps them nearly unique anyway).
    seen: list[str] = []
    for w in warnings:
        if w not in seen:
            seen.append(w)
    return lines, seen
