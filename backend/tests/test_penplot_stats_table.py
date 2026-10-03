"""Layer-stats overlay table: geometry units + HTTP behavior."""

from __future__ import annotations

import re

import pytest

from backend.penplot import stats_table
from backend.tests.helpers import default_params, png_bytes, upload

PAGE_W, PAGE_H, MARGIN = 210.0, 297.0, 10.0
ROWS = [("highways", 1026), ("roads", 16404)]


def _bbox(lines):
    xs = [x for pl in lines for x, _ in pl]
    ys = [y for pl in lines for _, y in pl]
    return min(xs), min(ys), max(xs), max(ys)


def _render(rows=ROWS, **kw):
    args = dict(position="top-right", page_w=PAGE_W, page_h=PAGE_H,
                margin_mm=MARGIN)
    args.update(kw)
    return stats_table.render_stats_table(rows, **args)


def test_empty_rows_is_noop():
    lines, warnings = _render([])
    assert lines == [] and warnings == []


def test_two_rows_draw_border_column_and_row_dividers():
    lines, warnings = _render()
    assert warnings == []
    # Border + column divider + 1 row divider + key/value text strokes.
    assert len(lines) >= 3 + 2
    border = lines[0]
    assert border[0] == border[-1] and len(border) == 5
    col = lines[1]
    assert len(col) == 2
    assert abs(col[0][0] - col[1][0]) < 1e-9  # vertical
    row = lines[2]
    assert len(row) == 2
    assert abs(row[0][1] - row[1][1]) < 1e-9  # horizontal


def test_key_capitalized_in_drawn_text_extent():
    # "roads" (5 narrow glyphs) vs "16404": the value column exists and
    # the table is wider than either bare string — i.e. two columns drew.
    lines, _ = _render([("roads", 16404)])
    x0, _, x1, _ = _bbox(lines)
    assert x1 - x0 > 10.0


def test_positions_anchor_corners():
    tl, _ = _render(position="top-left")
    tr, _ = _render(position="top-right")
    bl, _ = _render(position="bottom-left")
    br, _ = _render(position="bottom-right")
    tlx0, tly0, _, _ = _bbox(tl)
    _, try0, trx1, _ = _bbox(tr)
    blx0, _, _, bly1 = _bbox(bl)
    _, _, brx1, bry1 = _bbox(br)
    assert tlx0 < trx1 and abs(tly0 - try0) < 1e-6
    assert abs(tlx0 - blx0) < 1e-6 and tly0 < bly1
    assert abs(trx1 - brx1) < 1e-6 and try0 < bry1
    for lines in (tl, tr, bl, br):
        x0, y0, x1, y1 = _bbox(lines)
        assert x0 >= MARGIN and x1 <= PAGE_W - MARGIN
        assert y0 >= MARGIN and y1 <= PAGE_H - MARGIN


def test_bottom_sits_above_label_reserve():
    reserve = 12.0
    lines, _ = _render(position="bottom-right", reserve_bottom_mm=reserve)
    _, _, _, y1 = _bbox(lines)
    assert y1 <= PAGE_H - MARGIN - reserve + 1e-6


def test_unknown_position_falls_back_to_top_right():
    lines, _ = _render(position="middle-center")
    ref, _ = _render(position="top-right")
    assert _bbox(lines) == _bbox(ref)


# --- per-side pads (0-20 mm each), TC-D27 backend half -------------------
#
# Pads inset the rect the table is laid out in, so they move the drawn
# table inward on the anchored side(s) by exactly that many mm. They must
# not rescale, reflow or restyle the table when it already fits.


def test_pad_right_shifts_top_right_table_left_by_exact_mm():
    ref, _ = _render(position="top-right")
    _, ref_y0, ref_x1, _ = _bbox(ref)
    for pad in (1.0, 5.0, 10.0, 20.0):
        lines, _ = _render(position="top-right", pad_right_mm=pad)
        _, y0, x1, _ = _bbox(lines)
        assert ref_x1 - x1 == pytest.approx(pad, abs=1e-6), (
            f"pad_right={pad} moved the right edge by {ref_x1 - x1:.3f}mm"
        )
        assert y0 == pytest.approx(ref_y0, abs=1e-6)  # top edge is unchanged


def test_pad_left_shifts_top_left_table_right_by_exact_mm():
    ref, _ = _render(position="top-left")
    ref_x0, ref_y0, _, _ = _bbox(ref)
    for pad in (1.0, 5.0, 10.0, 20.0):
        lines, _ = _render(position="top-left", pad_left_mm=pad)
        x0, y0, _, _ = _bbox(lines)
        assert x0 - ref_x0 == pytest.approx(pad, abs=1e-6)
        assert y0 == pytest.approx(ref_y0, abs=1e-6)


def test_pad_top_shifts_top_anchored_table_down_by_exact_mm():
    ref, _ = _render(position="top-right")
    ref_x0, ref_y0, _, _ = _bbox(ref)
    for pad in (1.0, 5.0, 10.0, 20.0):
        lines, _ = _render(position="top-right", pad_top_mm=pad)
        x0, y0, _, _ = _bbox(lines)
        assert y0 - ref_y0 == pytest.approx(pad, abs=1e-6), (
            f"pad_top={pad} moved the top edge by {y0 - ref_y0:.3f}mm"
        )
        assert x0 == pytest.approx(ref_x0, abs=1e-6)  # left edge unchanged


def test_pad_bottom_shifts_bottom_anchored_table_up_by_exact_mm():
    ref, _ = _render(position="bottom-right")
    _, _, ref_x1, ref_y1 = _bbox(ref)
    for pad in (1.0, 5.0, 10.0, 20.0):
        lines, _ = _render(position="bottom-right", pad_bottom_mm=pad)
        _, _, x1, y1 = _bbox(lines)
        assert ref_y1 - y1 == pytest.approx(pad, abs=1e-6)
        assert x1 == pytest.approx(ref_x1, abs=1e-6)


def test_pads_zero_is_byte_identical_to_unpadded():
    ref, _ = _render(position="top-right")
    explicit, warnings = _render(
        position="top-right",
        pad_left_mm=0.0, pad_right_mm=0.0, pad_top_mm=0.0, pad_bottom_mm=0.0,
    )
    assert explicit == ref
    assert warnings == []


def test_pads_compose_on_the_anchored_side_only():
    # A top-left-anchored table's origin is inner_left, so pad_left moves
    # the whole table inward and pad_right is a no-op unless the box gets
    # tight enough to force a fit-scale. Two rows on A4 never get tight, so
    # the right pad must leave the artwork byte-identical.
    ref, _ = _render(position="top-left")
    ref_x0, _, ref_x1, _ = _bbox(ref)
    only_left, _ = _render(position="top-left", pad_left_mm=4.0)
    only_right, _ = _render(position="top-left", pad_right_mm=6.0)
    both, _ = _render(position="top-left", pad_left_mm=4.0, pad_right_mm=6.0)
    l_x0, _, l_x1, _ = _bbox(only_left)
    # Left pad shifts the left edge by exactly the pad; width unchanged.
    assert l_x0 == pytest.approx(ref_x0 + 4.0, abs=1e-6)
    assert (l_x1 - l_x0) == pytest.approx(ref_x1 - ref_x0, abs=1e-6)
    # Right pad alone is invisible to a left-anchored table.
    assert _bbox(only_right) == _bbox(ref)
    # The two compose to the left pad's effect (right pad still invisible).
    assert _bbox(both) == _bbox(only_left)


def test_oversized_pads_stay_on_page_and_do_not_crash():
    # Max pads (20 mm each side) on A4: the table is laid out in the
    # shrunken inner rect, stays on the page, and does not raise. It need
    # not fit-scale — the 2-row table still fits comfortably.
    lines, _ = _render(
        position="top-right",
        pad_left_mm=20.0, pad_right_mm=20.0, pad_top_mm=20.0, pad_bottom_mm=20.0,
    )
    assert lines
    x0, y0, x1, y1 = _bbox(lines)
    assert x0 >= MARGIN - 1e-6 and y0 >= MARGIN - 1e-6
    assert x1 <= PAGE_W - MARGIN + 1e-6 and y1 <= PAGE_H - MARGIN + 1e-6
    # Right pad pulled the right edge in by exactly 20 mm.
    ref, _ = _render(position="top-right")
    _, _, ref_x1, _ = _bbox(ref)
    assert ref_x1 - x1 == pytest.approx(20.0, abs=1e-6)


def test_pads_too_big_for_the_box_fit_scale_the_table_down():
    # The real fit-scaling path: pads + a tight inner rect shrink the box
    # below the natural table size, so the table must scale uniformly to
    # fit instead of overflowing the page. A small page forces this.
    tight_w, tight_h, tight_margin = 50.0, 50.0, 10.0
    natural, _ = stats_table.render_stats_table(
        ROWS, position="top-left", page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN)
    nat_x0, _, nat_x1, _ = _bbox(natural)
    scaled, _ = stats_table.render_stats_table(
        ROWS, position="top-left", page_w=tight_w, page_h=tight_h,
        margin_mm=tight_margin, pad_right_mm=8.0, pad_bottom_mm=8.0)
    assert scaled
    sx0, _, sx1, sy1 = _bbox(scaled)
    # Scaled to fit the 50mm page with an 8mm pad taken off the right.
    assert sx1 <= tight_w - tight_margin - 8.0 + 1e-6
    assert sy1 <= tight_h - tight_margin - 8.0 + 1e-6
    # Strictly narrower than the natural (unscaled) table.
    assert (sx1 - sx0) < (nat_x1 - nat_x0)


def test_pads_keep_table_inside_the_page_for_every_position():
    for position in ("top-left", "top-right", "bottom-left", "bottom-right"):
        for pad in (0.0, 7.0, 20.0):
            lines, _ = _render(
                position=position,
                pad_left_mm=pad, pad_right_mm=pad,
                pad_top_mm=pad, pad_bottom_mm=pad,
            )
            x0, y0, x1, y1 = _bbox(lines)
            assert x0 >= MARGIN - 1e-6 and y0 >= MARGIN - 1e-6, (position, pad)
            assert x1 <= PAGE_W - MARGIN + 1e-6 and y1 <= PAGE_H - MARGIN + 1e-6


def test_negative_pads_are_clamped_to_zero():
    # The renderer clamps with max(..., 0.0); a negative must not be able
    # to push the table outside the margin.
    ref, _ = _render(position="top-right")
    lines, _ = _render(position="top-right", pad_right_mm=-10.0, pad_top_mm=-10.0)
    assert _bbox(lines) == _bbox(ref)


def _convert(http_client, image_id: str, params: dict):
    return http_client.post("/v1/convert", json={"image_id": image_id, "params": params})


def _tabled(base: dict, **kw) -> dict:
    base["stats_table"] = {
        "enabled": True, "position": "top-right",
        "rows": [{"key": "highways", "value": 1026},
                 {"key": "roads", "value": 16404}],
    }
    base["stats_table"].update(kw)
    return base


def test_stats_table_adds_strokes_over_http(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    plain = _convert(http_client, image_id, default_params("hatch")).json()
    tabled = _convert(http_client, image_id, _tabled(default_params("hatch"))).json()
    assert tabled["stats"]["strokes"] > plain["stats"]["strokes"]
    svg = http_client.get(tabled["svg_url"])
    assert svg.status_code == 200 and "<path" in svg.text


def test_stats_table_disabled_is_noop_over_http(http_client):
    # Disabled-with-rows still salts the result cache key (rows are part of
    # the canonical params), so filenames differ — but the artwork must be
    # byte-identical and the stats equal.
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    plain = _convert(http_client, image_id, default_params("hatch")).json()
    off = _convert(
        http_client, image_id, _tabled(default_params("hatch"), enabled=False)).json()
    assert off["svg_url"] != plain["svg_url"]
    assert off["stats"] == plain["stats"]
    assert http_client.get(off["svg_url"]).text == http_client.get(plain["svg_url"]).text


def test_stats_table_positions_differ_over_http(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    urls = set()
    for position in ("top-left", "top-right", "bottom-left", "bottom-right"):
        body = _convert(
            http_client, image_id,
            _tabled(default_params("hatch"), position=position)).json()
        urls.add(body["svg_url"])
    assert len(urls) == 4


def test_stats_table_invalid_position_422(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = _tabled(default_params("hatch"), position="center")
    resp = _convert(http_client, image_id, params)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


# --- pads over the wire: schema gate + cache key ---------------------------


@pytest.mark.parametrize("side", ["left", "right", "top", "bottom"])
@pytest.mark.parametrize("bad", [20.5, 25.0, 100.0, -0.5, -5.0])
def test_stats_table_out_of_range_pads_422(http_client, side, bad):
    # The wire schema is the single validation gate (0-20 mm, extra=forbid),
    # so a bad pad must 422 rather than silently clamp.
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = _tabled(default_params("hatch"), **{f"pad_{side}_mm": bad})
    resp = _convert(http_client, image_id, params)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


@pytest.mark.parametrize("side", ["left", "right", "top", "bottom"])
def test_stats_table_pad_boundaries_accepted(http_client, side):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    for value in (0.0, 20.0):
        params = _tabled(default_params("hatch"), **{f"pad_{side}_mm": value})
        assert _convert(http_client, image_id, params).status_code == 200


def test_stats_table_unknown_pad_key_422(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = _tabled(default_params("hatch"), pad_sideways_mm=3.0)
    resp = _convert(http_client, image_id, params)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


def test_different_pads_produce_different_svg_urls(http_client):
    # The svg_url carries params_hash, so distinct pads must not share a
    # cache entry or a pad change would serve stale artwork.
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    urls = set()
    for pad in (0.0, 1.0, 5.0, 10.0):
        body = _convert(
            http_client, image_id,
            _tabled(default_params("hatch"), pad_right_mm=pad)).json()
        urls.add(body["svg_url"])
    assert len(urls) == 4


def test_identical_pads_hit_the_cache(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    a = _convert(http_client, image_id,
                 _tabled(default_params("hatch"), pad_right_mm=7.0)).json()
    b = _convert(http_client, image_id,
                 _tabled(default_params("hatch"), pad_right_mm=7.0)).json()
    assert a["svg_url"] == b["svg_url"]
    assert http_client.get(a["svg_url"]).text == http_client.get(b["svg_url"]).text


def test_pads_move_the_drawn_table_over_http(http_client):
    # End-to-end: the drawn table's right edge really moves by the pad.
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    plain = _convert(http_client, image_id,
                     _tabled(default_params("hatch"))).json()
    padded = _convert(http_client, image_id,
                      _tabled(default_params("hatch"), pad_right_mm=10.0)).json()
    assert padded["svg_url"] != plain["svg_url"]

    def table_x_max(url: str) -> float:
        svg = http_client.get(url).text
        return max(float(x) for x in re.findall(r"[ML]\s+(-?[\d.]+)\s+-?[\d.]+", svg))

    # The page is 210 mm wide; the padded table's right edge pulls in.
    assert table_x_max(padded["svg_url"]) < table_x_max(plain["svg_url"])
