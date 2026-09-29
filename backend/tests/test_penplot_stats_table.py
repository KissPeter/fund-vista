"""Layer-stats overlay table: geometry units + HTTP behavior."""

from __future__ import annotations

import math
import re

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


def _paths(svg_text):
    return re.findall(r"<path d=\"([^\"]+)\"/>", svg_text)


def _pts(d):
    nums = [float(v) for v in re.findall(r"(-?[\d.]+)", d)]
    return list(zip(nums[0::2], nums[1::2]))


def test_cover_zone_is_border_plus_pads():
    rows = [("highways", 1026), ("roads", 16404)]
    kw = dict(position="bottom-right", page_w=210.0, page_h=297.0,
              margin_mm=10.0, pad_left_mm=2.0, pad_right_mm=3.0,
              pad_top_mm=4.0, pad_bottom_mm=5.0)
    drawn, _ = stats_table.render_stats_table(rows, **kw)
    zone = stats_table.table_cover_zone(rows, **kw)
    assert zone is not None
    zx0, zy0, zx1, zy1 = zone
    # Drawn border corners sit exactly pads inside the zone edges.
    bx = [x for pl in drawn[:1] for x, _ in pl]
    by = [y for pl in drawn[:1] for _, y in pl]
    assert min(bx) - zx0 == 2.0 and zx1 - max(bx) == 3.0
    assert min(by) - zy0 == 4.0 and zy1 - max(by) == 5.0
    # Every drawn table stroke lives inside the zone (boundary inclusive).
    for pl in drawn:
        for x, y in pl:
            assert zx0 - 1e-9 <= x <= zx1 + 1e-9
            assert zy0 - 1e-9 <= y <= zy1 + 1e-9


def test_cover_zone_empty_is_none():
    assert stats_table.table_cover_zone(
        [], position="top-right", page_w=210.0, page_h=297.0, margin_mm=10.0,
    ) is None


DIAG = "\n".join(
    f'<path d="M 0 {y} L 210 {y}" stroke="#000000" fill="none"/>'
    for y in list(range(0, 298, 5)) + [297]
)
COMB = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="210" height="297">'
    + DIAG + "</svg>"
).encode()


def test_table_knocks_map_out_of_zone_over_http(http_client):
    """The table reads as if it had a background: comb teeth crossing its
    zone are cut at the edges. Survivors inside the zone must all be table
    furniture (border/dividers/text, taken from the draw call itself, so the
    assertion can never mistake the table for a street)."""
    table_kw = dict(position="bottom-right", pad_left_mm=10.0, pad_top_mm=8.0)
    rows = [("highways", 1026), ("roads", 16404)]
    drawn, _ = stats_table.render_stats_table(
        rows, page_w=210.0, page_h=297.0, margin_mm=10.0, **table_kw)
    furniture = {(round(x, 2), round(y, 2)) for pl in drawn for x, y in pl}
    zone = stats_table.table_cover_zone(
        rows, page_w=210.0, page_h=297.0, margin_mm=10.0, **table_kw)
    assert zone is not None
    x0, y0, x1, y1 = zone

    def unexplained(svg_text):
        bad = []
        for d in _paths(svg_text):
            pts = _pts(d)
            inside = [(x, y) for x, y in pts if x0 < x < x1 and y0 < y < y1]
            if not inside:
                continue
            xs = [x for x, _ in pts]
            ys = [y for _, y in pts]
            if math.hypot(max(xs) - min(xs), max(ys) - min(ys)) < 8.0:
                continue  # glyph-scale furniture
            if all(min(math.hypot(x - fx, y - fy) for fx, fy in furniture) <= 0.15
                   for x, y in inside):
                continue  # border/dividers
            bad.append(d)
        return bad

    image_id = upload(http_client, COMB, "v.svg").json()["image_id"]
    plain = _convert(http_client, image_id, default_params("contour")).json()
    tabled = _convert(
        http_client, image_id,
        _tabled(default_params("contour"), **table_kw)).json()
    assert unexplained(http_client.get(plain["svg_url"]).text) != []
    assert unexplained(http_client.get(tabled["svg_url"]).text) == []
