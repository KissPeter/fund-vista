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

import math

from backend.penplot.hershey_fonts import FACES
from backend.penplot.methods import Polyline
from backend.penplot.svgfont import FONT_ID as SVG_FONT_ID, get_svg_face

WARNING_UNSUPPORTED = "stats_table_unsupported_characters"

FONT = SVG_FONT_ID
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
    if FONT == SVG_FONT_ID:
        face = get_svg_face()
    else:
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


def _plan_table(
    rows: list[tuple[str, int]],
    *,
    position: str,
    page_w: float,
    page_h: float,
    margin_mm: float,
    reserve_bottom_mm: float = 0.0,
    pad_left_mm: float = 0.0,
    pad_right_mm: float = 0.0,
    pad_top_mm: float = 0.0,
    pad_bottom_mm: float = 0.0,
) -> dict | None:
    """Shared table geometry: border origin/size, fit, and laid-out text.

    Returns None for empty rows (silent no-op). One source for both the
    drawn strokes (``render_stats_table``) and the map-exclusion zone
    (``table_cover_zone``) so they can never disagree.
    """
    warnings: list[str] = []
    cells = [(_display_key(key), str(value)) for key, value in rows[:MAX_ROWS]]
    if not cells:
        return None

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
        return None
    if text_h <= 1e-9:
        text_h = HEIGHT_MM

    col0 = max(key_widths) + 2 * CELL_PAD_X_MM
    col1 = max(val_widths) + 2 * CELL_PAD_X_MM
    row_h = text_h + 2 * CELL_PAD_Y_MM
    table_w = col0 + col1
    table_h = row_h * len(cells)

    inner_left = margin_mm + TABLE_GAP_MM + max(pad_left_mm, 0.0)
    inner_right = page_w - margin_mm - TABLE_GAP_MM - max(pad_right_mm, 0.0)
    inner_top = margin_mm + TABLE_GAP_MM + max(pad_top_mm, 0.0)
    inner_bottom = (
        page_h - margin_mm - TABLE_GAP_MM - max(reserve_bottom_mm, 0.0) - max(pad_bottom_mm, 0.0)
    )
    inner_w = max(inner_right - inner_left, 1e-9)
    inner_h = max(inner_bottom - inner_top, 1e-9)

    # Oversized tables scale down uniformly to fit the inner rect.
    fit = min(1.0, inner_w / table_w, inner_h / table_h)

    pos = position if position in (
        "top-left", "top-right", "bottom-left", "bottom-right",
    ) else "top-right"
    x0 = inner_left if pos.endswith("left") else inner_right - table_w * fit
    y0 = inner_top if pos.startswith("top") else inner_bottom - table_h * fit
    return {
        "warnings": warnings, "cells": cells, "text_blocks": text_blocks,
        "text_h": text_h, "col0": col0, "row_h": row_h,
        "table_w": table_w, "table_h": table_h, "fit": fit,
        "x0": x0, "y0": y0,
    }


def table_cover_zone(
    rows: list[tuple[str, int]],
    *,
    position: str,
    page_w: float,
    page_h: float,
    margin_mm: float,
    reserve_bottom_mm: float = 0.0,
    pad_left_mm: float = 0.0,
    pad_right_mm: float = 0.0,
    pad_top_mm: float = 0.0,
    pad_bottom_mm: float = 0.0,
) -> tuple[float, float, float, float] | None:
    """Map-exclusion zone for the table: border rect expanded by the pads.

    Image geometry inside is knocked out by the pipeline so the table never
    overplots the map — the pads read as clear space on every side. None
    when the table itself is a no-op (same empty-rows rule as the draw).
    """
    plan = _plan_table(
        rows, position=position, page_w=page_w, page_h=page_h,
        margin_mm=margin_mm, reserve_bottom_mm=reserve_bottom_mm,
        pad_left_mm=pad_left_mm, pad_right_mm=pad_right_mm,
        pad_top_mm=pad_top_mm, pad_bottom_mm=pad_bottom_mm,
    )
    if plan is None:
        return None
    fit = plan["fit"]
    x0 = plan["x0"] - max(pad_left_mm, 0.0)
    y0 = plan["y0"] - max(pad_top_mm, 0.0)
    x1 = plan["x0"] + plan["table_w"] * fit + max(pad_right_mm, 0.0)
    y1 = plan["y0"] + plan["table_h"] * fit + max(pad_bottom_mm, 0.0)
    return (x0, y0, x1, y1)


def exclude_rect(
    polylines: list[list[tuple[float, float]]],
    rect: tuple[float, float, float, float],
) -> list[list[tuple[float, float]]]:
    """Drop the parts of polylines falling inside an axis-aligned rect.

    Segment-exact (Liang-Barsky complement): runs outside the rect survive
    as split polylines, so streets crossing the zone visibly stop at its
    edge instead of vanishing whole. Points exactly on the edge count as
    outside (stable across quantize).
    """
    x0, y0, x1, y1 = rect
    kept: list[list[tuple[float, float]]] = []
    for pts in polylines:
        if len(pts) < 2:
            continue
        cur: list[tuple[float, float]] = []

        def flush() -> None:
            if len(cur) > 1:
                kept.append(cur)

        for p0, p1 in zip(pts, pts[1:]):
            # Inside-interval of this segment (Liang-Barsky); None when
            # fully outside.
            dx, dy = p1[0] - p0[0], p1[1] - p0[1]
            t0, t1 = 0.0, 1.0
            valid = True
            for p, q in ((-dx, p0[0] - x0), (dx, x1 - p0[0]),
                         (-dy, p0[1] - y0), (dy, y1 - p0[1])):
                if abs(p) < 1e-12:
                    if q < 0.0:
                        valid = False
                        break
                else:
                    r = q / p
                    if p < 0.0:
                        t0 = max(t0, r)
                    else:
                        t1 = min(t1, r)
                    if t0 > t1:
                        valid = False
                        break
            # Kept sub-runs: complement of the inside-interval. Fully
            # outside keeps the whole segment (chains); fully inside keeps
            # nothing (flushes the open run across the gap).
            parts = [(0.0, 1.0)] if not valid else [
                (a, b) for a, b in ((0.0, t0), (t1, 1.0)) if b - a >= 1e-9
            ]
            if not parts:
                flush()
                cur = []
                continue
            for a, b in parts:
                q0 = (p0[0] + a * dx, p0[1] + a * dy)
                q1 = (p0[0] + b * dx, p0[1] + b * dy)
                if cur and math.hypot(q0[0] - cur[-1][0], q0[1] - cur[-1][1]) <= 1e-9:
                    cur.append(q1)
                else:
                    flush()
                    cur = [q0, q1]
        flush()
    return kept


def render_stats_table(
    rows: list[tuple[str, int]],
    *,
    position: str,
    page_w: float,
    page_h: float,
    margin_mm: float,
    reserve_bottom_mm: float = 0.0,
    pad_left_mm: float = 0.0,
    pad_right_mm: float = 0.0,
    pad_top_mm: float = 0.0,
    pad_bottom_mm: float = 0.0,
) -> tuple[list[Polyline], list[str]]:
    """Lay out the stats table in mm space. See module docstring.

    ``rows`` are (raw layer key, path count) pairs — already filtered to
    non-zero layers by the caller. ``position`` is one of ``top-left``,
    ``top-right``, ``bottom-left``, ``bottom-right`` (anything else falls
    back to ``top-right``).
    """
    plan = _plan_table(
        rows, position=position, page_w=page_w, page_h=page_h,
        margin_mm=margin_mm, reserve_bottom_mm=reserve_bottom_mm,
        pad_left_mm=pad_left_mm, pad_right_mm=pad_right_mm,
        pad_top_mm=pad_top_mm, pad_bottom_mm=pad_bottom_mm,
    )
    if plan is None:
        return [], []
    warnings = list(plan["warnings"])
    cells = plan["cells"]
    text_blocks = plan["text_blocks"]
    text_h = plan["text_h"]
    col0 = plan["col0"]
    row_h = plan["row_h"]
    table_w = plan["table_w"]
    table_h = plan["table_h"]
    fit = plan["fit"]
    x0 = plan["x0"]
    y0 = plan["y0"]

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
