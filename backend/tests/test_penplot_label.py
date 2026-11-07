"""Title-block labels: Hershey geometry units + HTTP behavior."""

from __future__ import annotations

import statistics
import time

import pytest
from pydantic import ValidationError

from backend.penplot import labels
from backend.penplot.pipeline import effective_params_dump, params_hash
from backend.penplot.schemas import ConvertParams, LabelParams
from backend.penplot.stages.layout_svg import layout, layout_scale
from backend.tests.helpers import default_params, png_bytes, upload

PAGE_W, PAGE_H, MARGIN = 210.0, 297.0, 10.0


def _bbox(lines):
    xs = [x for pl in lines for x, _ in pl]
    ys = [y for pl in lines for _, y in pl]
    return min(xs), min(ys), max(xs), max(ys)


def test_align_left_starts_at_margin():
    lines, warnings = labels.render_label(
        "AB", height_mm=5.0, align="left",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=False,
    )
    assert warnings == []
    x0, _, _, _ = _bbox(lines)
    assert abs(x0 - MARGIN) < 1e-6


def test_align_right_ends_at_margin():
    lines, _ = labels.render_label(
        "AB", height_mm=5.0, align="right",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=False,
    )
    _, _, x1, _ = _bbox(lines)
    assert abs(x1 - (PAGE_W - MARGIN)) < 1e-6


def test_align_center_is_centred_between_margins():
    lines, _ = labels.render_label(
        "AB", height_mm=5.0, align="center",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=False,
    )
    x0, _, x1, _ = _bbox(lines)
    assert abs((x0 + x1) / 2.0 - PAGE_W / 2.0) < 1e-6


def test_align_fill_spans_inner_width():
    lines, _ = labels.render_label(
        "AB", height_mm=5.0, align="fill",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=False,
    )
    x0, _, x1, _ = _bbox(lines)
    assert abs(x0 - MARGIN) < 1e-6
    assert abs(x1 - (PAGE_W - MARGIN)) < 1e-6


def test_label_sits_inside_bottom_margin():
    # Comma tail hangs below the cap baseline - anchoring uses the real bbox.
    lines, _ = labels.render_label(
        "A,", height_mm=5.0, align="right",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=False,
    )
    _, _, _, y1 = _bbox(lines)
    assert abs(y1 - (PAGE_H - MARGIN)) < 1e-6


def test_empty_text_is_noop_and_unknown_chars_warn():
    lines, warnings = labels.render_label(
        "   ", height_mm=5.0, align="right",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=False,
    )
    assert lines == [] and warnings == []
    lines, warnings = labels.render_label(
        "Aéb", height_mm=5.0, align="right",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=False, font="futural",
    )
    assert len(lines) > 0  # the A still draws
    assert any(w.startswith("label_unsupported_characters") for w in warnings)


def test_border_draws_title_block_band():
    plain, _ = labels.render_label(
        "AB", height_mm=5.0, align="right",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=False,
    )
    boxed, _ = labels.render_label(
        "AB", height_mm=5.0, align="right",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=True, border_radius_mm=0.0,
    )
    # One extra polyline: the band. The page frame is NOT drawn here any more.
    assert len(boxed) == len(plain) + 1
    band = boxed[-1]
    assert len(band) == 5 and band[0] == band[-1]
    # The band spans the full inner width (no pads here: same thing).
    assert abs(band[0][0] - MARGIN) < 1e-6
    assert abs(band[1][0] - (PAGE_W - MARGIN)) < 1e-6
    # Its bottom edge is the bottom margin; its top edge sits above the text.
    px0, py0, px1, py1 = _bbox(plain)
    bx0, by0, bx1, by1 = _bbox(boxed)
    assert abs(by1 - (PAGE_H - MARGIN)) < 1e-6
    assert by0 < py0
    # The band is a bottom strip, never the whole-page margin rect.
    assert by0 > MARGIN
    assert bx0 == MARGIN and bx1 == PAGE_W - MARGIN
    # The lettering sits inside the band.
    gx0, gy0, gx1, gy1 = _bbox(boxed[:-1])
    assert gy0 > by0 and gy1 < by1


def test_border_band_spans_the_full_inner_width_pads_inset_text_only():
    # Pads inset the lettering only: the band always spans the full inner
    # width so its top edge lands square on the page-frame verticals (the
    # shop regression: the band stopped short of the frame on the right and
    # grew a radius where it should have met it).
    lines, _ = labels.render_label(
        "AB", height_mm=5.0, align="right",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=True, border_radius_mm=0.0,
        pad_left_mm=20.0, pad_right_mm=10.0,
    )
    band, text = lines[-1], lines[:-1]
    assert abs(band[0][0] - MARGIN) < 1e-6
    assert abs(band[1][0] - (PAGE_W - MARGIN)) < 1e-6
    tx0, _, tx1, _ = _bbox(text)
    assert abs(tx1 - (PAGE_W - MARGIN - 10.0)) < 1e-6  # right-aligned
    assert tx0 >= MARGIN + 20.0 - 1e-6


def test_band_with_pads_and_unequal_radii():
    # The frame/band interplay the HTTP cases never exercise: non-zero pads
    # and label.border_radius_mm != page.frame_radius_mm. The band still
    # spans the full inner width (riding the frame verticals) and follows
    # its OWN radius at the bottom - the corners coincide with the frame's
    # only when a caller sets the two radii equal (accepted outcome: with
    # both on, the band re-inks the frame's lower half).
    lines, _ = labels.render_label(
        "AB", height_mm=5.0, align="right",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=True, border_radius_mm=3.0,
        pad_left_mm=4.0, pad_right_mm=5.0,
    )
    band = lines[-1]
    assert band[0] == band[-1] and band[0] == (MARGIN, band[0][1])
    xs = [x for x, _ in band]
    assert abs(min(xs) - MARGIN) < 1e-6
    assert abs(max(xs) - (PAGE_W - MARGIN)) < 1e-6
    # Bottom corners follow the band's own radius (3.0), not the frame's.
    assert any(
        MARGIN < x < MARGIN + 3.0 and y > PAGE_H - MARGIN - 3.0
        for x, y in band
    )
    # The lettering stays inside the pads.
    tx1 = max(x for pl in lines[:-1] for x, _ in pl)
    assert abs(tx1 - (PAGE_W - MARGIN - 5.0)) < 1e-6


def test_futural_draws_lowercase_simplex_warns():
    fut, fut_w = labels.render_label(
        "ab", height_mm=5.0, align="left",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        font="futural", border=False,
    )
    assert fut and fut_w == []
    sim, sim_w = labels.render_label(
        "ab", height_mm=5.0, align="left",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        font="simplex", border=False,
    )
    assert sim == []  # simplex has no lowercase at all
    assert any(w.startswith("label_unsupported_characters") for w in sim_w)


def test_faces_differ_in_stroke_count():
    fut, _ = labels.render_label(
        "Ag", height_mm=5.0, align="left",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        font="futural", border=False,
    )
    sim, sim_w = labels.render_label(
        "Ag", height_mm=5.0, align="left",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        font="simplex", border=False,
    )
    # Simplex has no lowercase g: fewer strokes + a warning.
    assert len(fut) > len(sim)
    assert any(w.startswith("label_unsupported_characters") for w in sim_w)


def test_pad_left_insets_left_aligned_text():
    lines, _ = labels.render_label(
        "AB", height_mm=5.0, align="left",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=False, pad_left_mm=5.0, pad_right_mm=0.0,
    )
    x0, _, _, _ = _bbox(lines)
    assert abs(x0 - (MARGIN + 5.0)) < 1e-6


def test_pad_right_insets_right_aligned_text():
    lines, _ = labels.render_label(
        "AB", height_mm=5.0, align="right",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=False, pad_left_mm=0.0, pad_right_mm=4.0,
    )
    _, _, x1, _ = _bbox(lines)
    assert abs(x1 - (PAGE_W - MARGIN - 4.0)) < 1e-6


def test_fill_spans_padded_slot_and_band_spans_full_width():
    lines, _ = labels.render_label(
        "AB", height_mm=5.0, align="fill",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=True, border_radius_mm=0.0,
        pad_left_mm=5.0, pad_right_mm=7.0,
    )
    band = lines[-1]
    # The band spans the full inner width; only the lettering is inset.
    assert abs(band[0][0] - MARGIN) < 1e-6
    assert abs(band[1][0] - (PAGE_W - MARGIN)) < 1e-6
    # And the text itself stays inside the padded slot.
    text_lines = lines[:-1]
    x0, _, x1, _ = _bbox(text_lines)
    assert x0 >= MARGIN + 5.0 - 1e-6
    assert x1 <= PAGE_W - MARGIN - 7.0 + 1e-6


def test_rounded_band_defaults_and_clamps():
    boxed, _ = labels.render_label(
        "AB", height_mm=5.0, align="right",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=True,  # default radius 2.0
    )
    band = boxed[-1]
    # Straight top edge + two rounded bottom corners (8 chords each) + close.
    assert len(band) == 2 + 2 * 8 + 1
    assert band[0] == band[-1]
    # Square top corners exactly on the inner width.
    assert abs(band[0][0] - MARGIN) < 1e-6
    assert abs(band[1][0] - (PAGE_W - MARGIN)) < 1e-6
    for x, y in band:
        assert MARGIN - 1e-6 <= x <= PAGE_W - MARGIN + 1e-6
        assert MARGIN - 1e-6 <= y <= PAGE_H - MARGIN + 1e-6
    # A bottom strip, not the whole-page frame.
    ys = [y for _, y in band]
    assert max(ys) - min(ys) < 30.0
    # Absurd radius clamps to half the band's smaller side, never inverts.
    huge, _ = labels.render_label(
        "AB", height_mm=5.0, align="right",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=True, border_radius_mm=500.0,
    )
    hband = huge[-1]
    assert hband[0] == hband[-1]
    for x, y in hband:
        assert MARGIN - 1e-6 <= x <= PAGE_W - MARGIN + 1e-6
        assert MARGIN - 1e-6 <= y <= PAGE_H - MARGIN + 1e-6


def test_band_top_edge_is_square_on_the_frame_verticals():
    # Regression (shop report, 2026-10-27): the band grew rounded top corners
    # where it should meet the page frame, and stopped short of the right
    # vertical by the pad. The top edge must be one straight full-width
    # segment meeting the frame verticals at right angles; only the bottom
    # corners are rounded, and they coincide with the frame's own bottom
    # corners so the band hugs the sheet instead of squaring it off.
    boxed, _ = labels.render_label(
        "AB", height_mm=5.0, align="right",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=True, border_radius_mm=2.0,
        pad_left_mm=0.0, pad_right_mm=5.0,
    )
    band = boxed[-1]
    band_top = band[0][1]
    # Square top corners, full width (pad_right must NOT inset the band).
    assert band[0] == (MARGIN, band_top)
    assert band[1] == (PAGE_W - MARGIN, band_top)
    # The rounded part is only at the bottom: arc chords hug the sheet
    # corners (x inside the corner radius at the very bottom).
    assert any(
        MARGIN < x < MARGIN + 2.0 and y > PAGE_H - MARGIN - 2.0
        for x, y in band
    )
    # The band's bottom corners coincide with the page frame's bottom
    # corners (same centres/radii/chords), so the two never disagree.
    frame = labels.page_frame_rect(PAGE_W, PAGE_H, MARGIN, 2.0)
    frame_bottom = {
        (round(x, 6), round(y, 6))
        for x, y in frame if y >= PAGE_H - MARGIN - 2.0
    }
    for x, y in band:
        if y >= PAGE_H - MARGIN - 2.0:
            assert (round(x, 6), round(y, 6)) in frame_bottom


def _convert(http_client, image_id: str, params: dict):
    return http_client.post("/v1/convert", json={"image_id": image_id, "params": params})


def _labeled(base: dict, **kw) -> dict:
    base["label"] = {
        "enabled": True, "text": "A-1", "align": "right",
        "height_mm": 5.0, "font": "futural", "border": True,
    }
    base["label"].update(kw)
    return base


def test_label_adds_strokes_over_http(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    plain = _convert(http_client, image_id, default_params("hatch")).json()
    stamped = _convert(http_client, image_id, _labeled(default_params("hatch"))).json()
    assert stamped["stats"]["strokes"] > plain["stats"]["strokes"]
    svg = http_client.get(stamped["svg_url"])
    assert svg.status_code == 200 and "<path" in svg.text


def test_label_alignments_differ_over_http(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    urls = set()
    for align in ("left", "right", "fill"):
        body = _convert(http_client, image_id, _labeled(default_params("hatch"), align=align)).json()
        urls.add(body["svg_url"])
    assert len(urls) == 3


def test_label_warnings_surfaced_over_http(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    body = _convert(
        http_client, image_id, _labeled(default_params("hatch"), text="Aéb")
    ).json()
    assert any("label_unsupported_characters" in w for w in body["warnings"])


def test_label_invalid_align_422(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = _labeled(default_params("hatch"), align="diagonal")
    resp = _convert(http_client, image_id, params)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


def test_label_border_adds_stroke_over_http(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    open_box = _convert(
        http_client, image_id, _labeled(default_params("hatch"), border=False)
    ).json()
    boxed = _convert(
        http_client, image_id, _labeled(default_params("hatch"), border=True)
    ).json()
    assert boxed["svg_url"] != open_box["svg_url"]
    # The band is one stroke (the old strip was a frame + a divider).
    assert boxed["stats"]["strokes"] == open_box["stats"]["strokes"] + 1


def test_label_fonts_differ_over_http(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    urls = set()
    for font in ("futural", "futuram", "simplex",
                 "excalifont", "comic-shanns", "nunito"):
        body = _convert(
            http_client, image_id, _labeled(default_params("hatch"), font=font)
        ).json()
        urls.add(body["svg_url"])
    assert len(urls) == 6


def test_label_invalid_font_422(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = _labeled(default_params("hatch"), font="comic-sans")
    resp = _convert(http_client, image_id, params)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


def test_outline_faces_draw_lowercase_and_accents():
    # Hybrid promise: what simplex warns on, outlines draw clean.
    for font in ("excalifont", "comic-shanns", "nunito"):
        lines, warnings = labels.render_label(
            "Agé", height_mm=5.0, align="left",
            page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
            font=font, border=False,
        )
        assert len(lines) > 0, font
        assert warnings == [], (font, warnings)


def test_outline_faces_draw_more_ink_than_simplex():
    # Outlines trace both sides of every stem as curves: fewer loops than
    # Hershey strokes, but strictly more plotted points.
    def _points(lines):
        return sum(len(pl) for pl in lines)
    sim, _ = labels.render_label(
        "AG", height_mm=5.0, align="left",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        font="simplex", border=False,
    )
    for font in ("excalifont", "comic-shanns", "nunito"):
        out, warnings = labels.render_label(
            "AG", height_mm=5.0, align="left",
            page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
            font=font, border=False,
        )
        assert warnings == [], (font, warnings)
        assert _points(out) > _points(sim), (font, _points(out), _points(sim))


def test_outline_reserve_matches_band_top():
    lines, _ = labels.render_label(
        "Ag", height_mm=5.0, align="right",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        font="nunito", border=True, border_radius_mm=0.0,
    )
    band = lines[-1]
    reserve = labels.label_reserve_mm(
        "Ag", height_mm=5.0, font="nunito", border=True)
    assert reserve > 0.0
    band_top = min(y for _, y in band)
    assert abs(band_top - (PAGE_H - MARGIN - reserve)) < 1e-6


def test_label_padding_differ_over_http(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    urls = set()
    for pads in ({"pad_left_mm": 0.0, "pad_right_mm": 0.0},
                 {"pad_left_mm": 5.0, "pad_right_mm": 0.0},
                 {"pad_left_mm": 0.0, "pad_right_mm": 6.0}):
        body = _convert(
            http_client, image_id, _labeled(default_params("hatch"), **pads)
        ).json()
        urls.add(body["svg_url"])
    assert len(urls) == 3


def test_label_excessive_padding_422(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = _labeled(default_params("hatch"), pad_left_mm=99.0)
    resp = _convert(http_client, image_id, params)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


def test_label_border_radius_differ_over_http(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    urls = set()
    for radius in (0.0, 5.0):
        body = _convert(
            http_client, image_id,
            _labeled(default_params("hatch"), border_radius_mm=radius),
        ).json()
        urls.add(body["svg_url"])
    assert len(urls) == 2


def test_label_reserve_matches_band_top():
    # Image area bottom (page_h - margin - reserve) must equal the top edge
    # of the title-block band, so artwork bottoms out exactly on the band.
    for border in (False, True):
        reserve = labels.label_reserve_mm(
            "AB", height_mm=5.0, font="futural", border=border,
        )
        assert reserve > 0.0
        lines, _ = labels.render_label(
            "AB", height_mm=5.0, align="right",
            page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
            border=border, border_radius_mm=0.0, font="futural",
        )
        if border:
            band_top = min(y for _, y in lines[-1])
            assert abs(band_top - (PAGE_H - MARGIN - reserve)) < 1e-6
        else:
            _, y_text_top, _, _ = _bbox(lines)
            assert abs(y_text_top - (PAGE_H - MARGIN - reserve)) < 1e-6


def test_label_reserve_zero_for_blank_and_unsupported():
    assert labels.label_reserve_mm(
        "   ", height_mm=5.0, font="futural", border=True) == 0.0
    assert labels.label_reserve_mm(
        "é€", height_mm=5.0, font="futural", border=True) == 0.0


def test_layout_reserve_confines_wide_artwork():
    from backend.penplot.optimize import layout

    # Panorama source: without a reserve it would span the full inner height.
    raw = [[(0.0, 0.0), (400.0, 60.0)], [(0.0, 60.0), (400.0, 0.0)]]
    reserve = labels.label_reserve_mm(
        "SCALE 1:50", height_mm=5.0, font="futural", border=True,
    )
    laid, page_w, page_h = layout(
        raw, 400.0, 60.0, size="A4", orientation="portrait",
        margin_mm=MARGIN,
        reserve_bottom_mm=reserve + labels.LABEL_ARTWORK_GAP_MM,
    )
    floor = page_h - MARGIN - reserve - labels.LABEL_ARTWORK_GAP_MM
    for pl in laid:
        for _, y in pl:
            assert y <= floor + 1e-6


def test_panorama_with_label_converts_over_http(http_client):
    image_id = upload(http_client, png_bytes(400, 60)).json()["image_id"]
    body = _convert(
        http_client, image_id,
        _labeled(default_params("hatch"), text="SCALE 1:50"),
    ).json()
    svg = http_client.get(body["svg_url"])
    assert svg.status_code == 200 and "<path" in svg.text


def test_solid_panorama_stays_above_the_band_over_http(http_client):
    import io
    import re

    from PIL import Image as PILImage

    # Worst case: ink everywhere - hatch fills the whole image area.
    img = PILImage.new("RGB", (400, 60), "black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    image_id = upload(http_client, buf.getvalue()).json()["image_id"]
    label = {"enabled": True, "text": "SCALE 1:50", "align": "right",
             "height_mm": 5.0, "font": "futural", "border": True,
             "pad_left_mm": 0.0, "pad_right_mm": 0.0, "border_radius_mm": 0.0}
    params = default_params("hatch")
    params["label"] = label
    params["page"] = {"size": "A4", "orientation": "portrait", "margin_mm": 10.0,
                      "frame": True, "frame_radius_mm": 0.0}
    body = http_client.post(
        "/v1/convert", json={"image_id": image_id, "params": params}).json()
    svg = http_client.get(body["svg_url"]).text
    band_top = 297.0 - 10.0 - labels.label_reserve_mm(
        "SCALE 1:50", height_mm=5.0, font="futural", border=True)
    paths = re.findall(r"<path d=\"([^\"]+)\"/>", svg)
    assert paths
    spanning = 0
    for d in paths:
        ys = [float(v) for v in re.findall(r"[ML]\s+(-?[\d.]+)\s+(-?[\d.]+)", d)
              for v in [v[1]]]
        # Spans across the band top: reaches into the image zone above AND the
        # band below. Label text lives wholly below; artwork wholly above.
        if min(ys) < band_top - 0.5 and max(ys) > band_top + 0.5:
            spanning += 1
    # Only the page frame spans the band top; nothing overflows the strip.
    assert spanning == 1


def test_strip_bottom_gap_clears_pen_width():
    # Descenders must not touch the frame bottom: gap is at least the pad.
    for font in labels.LABEL_FONTS:
        m = labels._strip_metrics("Budapest Liszt Ferenc", height_mm=5.0, font=font)
        assert m is not None
        _, pad, gap = m
        assert gap >= pad and gap >= labels.BORDER_GAP_MIN_MM


# -- title placement outside the border (below / above) -----------------------

SHEETS = ((210.0, 297.0), (297.0, 210.0))  # A4 portrait, A4 landscape


def _ys(lines):
    return [y for pl in lines for _, y in pl]


def _ink_h(lines):
    ys = _ys(lines)
    return max(ys) - min(ys)


def _outside(text, placement, subtitle="", *, w=PAGE_W, h=PAGE_H, height=5.0,
             align="right"):
    return labels.render_label_rows(
        text, height_mm=height, align=align, page_w=w, page_h=h, margin_mm=MARGIN,
        placement=placement, subtitle=subtitle,
    )


def test_inside_default_is_explicit_inside_and_joins_subtitle():
    default, _ = labels.render_label(
        "AB - CD", height_mm=5.0, align="right", page_w=PAGE_W, page_h=PAGE_H,
        margin_mm=MARGIN,
    )
    explicit, _ = labels.render_label(
        "AB", subtitle="CD", height_mm=5.0, align="right", page_w=PAGE_W,
        page_h=PAGE_H, margin_mm=MARGIN, placement="inside",
    )
    assert default == explicit


def test_unknown_placement_is_rejected():
    with pytest.raises(ValidationError):
        LabelParams.model_validate({"placement": "left"})
    with pytest.raises(ValueError):
        _outside("AB", "inside")


def test_label_lines_are_capped_and_checked():
    assert LabelParams.model_validate({"lines": ["Title", "Sub"]}).lines == ["Title", "Sub"]
    for bad in (["a", "b", "c"], ["x" * 42], ["bad\nline"]):
        with pytest.raises(ValidationError):
            LabelParams.model_validate({"lines": bad})


def test_cache_key_drops_only_the_inside_placement():
    base = ConvertParams.model_validate(default_params("hatch") | {"label": {"enabled": True, "text": "T"}})
    inside = ConvertParams.model_validate(
        default_params("hatch") | {"label": {"enabled": True, "text": "T", "placement": "inside"}})
    below = ConvertParams.model_validate(
        default_params("hatch") | {"label": {"enabled": True, "text": "T", "placement": "below"}})
    assert effective_params_dump(inside, False) == effective_params_dump(base, False)
    assert "placement" not in effective_params_dump(base, False)["label"]
    assert effective_params_dump(below, False)["label"]["placement"] == "below"
    assert params_hash(base) == params_hash(inside)
    assert params_hash(below) != params_hash(base)


def test_outside_render_is_deterministic():
    assert _outside("Budapest", "below", "Map") == _outside("Budapest", "below", "Map")
    assert _outside("Budapest", "above", "Map") == _outside("Budapest", "above", "Map")


@pytest.mark.parametrize("placement", ["below", "above"])
def test_outside_lettering_sits_on_the_margin(placement):
    rows, _ = _outside("Budapest", placement, "Map 1:5000")
    title, sub = rows
    assert max(_ys(title)) < min(_ys(sub))  # title first, subtitle after it
    if placement == "below":
        assert abs(max(_ys(sub)) - (PAGE_H - MARGIN)) < 1e-6
    else:
        assert abs(min(_ys(title)) - MARGIN) < 1e-6


@pytest.mark.parametrize("placement", ["below", "above"])
def test_outside_has_no_band(placement):
    plain, _ = labels.render_label(
        "AB", subtitle="CD", height_mm=5.0, align="right", page_w=PAGE_W,
        page_h=PAGE_H, margin_mm=MARGIN, placement=placement, border=False,
    )
    boxed, _ = labels.render_label(
        "AB", subtitle="CD", height_mm=5.0, align="right", page_w=PAGE_W,
        page_h=PAGE_H, margin_mm=MARGIN, placement=placement, border=True,
    )
    assert plain == boxed


@pytest.mark.parametrize("placement", ["below", "above"])
def test_frame_inset_equals_the_strip(placement):
    strip = labels.label_outside_mm("Budapest", subtitle="Map", height_mm=5.0)
    if placement == "below":
        frame = labels.page_frame_rect(
            PAGE_W, PAGE_H, MARGIN, 2.0, bottom_mm=PAGE_H - MARGIN - strip)
        assert abs(max(_ys([frame])) - (PAGE_H - MARGIN - strip)) < 1e-6
        assert abs(min(_ys([frame])) - MARGIN) < 1e-6
        rows, _ = _outside("Budapest", placement, "Map")
        assert min(_ys(rows[0] + rows[1])) > PAGE_H - MARGIN - strip
    else:
        frame = labels.page_frame_rect(
            PAGE_W, PAGE_H, MARGIN, 2.0, top_mm=MARGIN + strip)
        assert abs(min(_ys([frame])) - (MARGIN + strip)) < 1e-6
        assert abs(max(_ys([frame])) - (PAGE_H - MARGIN)) < 1e-6
        rows, _ = _outside("Budapest", placement, "Map")
        assert max(_ys(rows[0] + rows[1])) < MARGIN + strip


def test_strip_is_pad_title_gap_subtitle_pad():
    h = 5.0
    rows, _ = _outside("Budapest", "below", "Map", height=h)
    title_h, sub_h = _ink_h(rows[0]), _ink_h(rows[1])
    pad = max(h * labels.BORDER_PAD_RATIO, labels.BORDER_PAD_MIN_MM)
    both = labels.label_outside_mm("Budapest", subtitle="Map", height_mm=h)
    assert abs(both - (2 * pad + title_h + labels.OUTSIDE_LINE_GAP_MM + sub_h)) < 1e-6
    only = labels.label_outside_mm("Budapest", height_mm=h)
    assert abs(only - (2 * pad + title_h)) < 1e-6
    assert labels.label_outside_mm("", height_mm=h) == 0.0


def test_subtitle_cap_height_is_six_tenths_of_the_title():
    rows, _ = _outside("H", "below", "H")
    assert abs(_ink_h(rows[1]) / _ink_h(rows[0]) - 0.6) < 1e-6
    assert labels.SUBTITLE_HEIGHT_RATIO == 0.6


def test_no_subtitle_draws_one_line_and_a_shorter_strip():
    rows, _ = _outside("Budapest", "above")
    assert len(rows) == 1
    with_sub = labels.label_outside_mm("Budapest", subtitle="Map", height_mm=5.0)
    assert labels.label_outside_mm("Budapest", height_mm=5.0) < with_sub


@pytest.mark.parametrize("placement", ["below", "above"])
def test_artwork_keeps_clear_of_the_title_strip(placement):
    strip = labels.label_outside_mm("Budapest", subtitle="Map", height_mm=5.0)
    reserve = strip + labels.LABEL_ARTWORK_GAP_MM
    art = [[(0.0, 0.0), (400.0, 0.0), (400.0, 200.0), (0.0, 200.0), (0.0, 0.0)]]
    key = "reserve_bottom_mm" if placement == "below" else "reserve_top_mm"
    laid, _, _ = layout(
        art, 400, 200, size="A4", orientation="portrait", margin_mm=MARGIN,
        padding_mm=5.0, **{key: reserve},
    )
    if placement == "below":
        assert max(_ys(laid)) <= PAGE_H - MARGIN - strip
    else:
        assert min(_ys(laid)) >= MARGIN + strip


def test_layout_scale_matches_layout_with_a_top_reserve():
    # Tall artwork: the height, not the width, limits the scale.
    art = [[(0.0, 0.0), (200.0, 0.0), (200.0, 1000.0), (0.0, 1000.0), (0.0, 0.0)]]
    laid, _, _ = layout(
        art, 200, 1000, size="A4", orientation="portrait", margin_mm=MARGIN,
        padding_mm=5.0, reserve_top_mm=20.0,
    )
    xs = [x for x, _ in laid[0]]
    scale = layout_scale(200, 1000, "A4", "portrait", MARGIN, 0.0, 5.0, 20.0)
    assert abs((max(xs) - min(xs)) / 200.0 - scale) < 1e-6
    assert scale < layout_scale(200, 1000, "A4", "portrait", MARGIN, 0.0, 5.0)


def test_unsupported_glyph_in_subtitle_still_warns():
    _, warnings = _outside("Budapest", "below", "Map\u2603")
    assert any(w.startswith(labels.WARNING_UNSUPPORTED) for w in warnings)


@pytest.mark.parametrize("w,h", SHEETS)
@pytest.mark.parametrize("placement", ["below", "above"])
@pytest.mark.parametrize("subtitle", ["", "W" * 41])
@pytest.mark.parametrize("align", ["left", "right", "fill"])
def test_outside_stays_inside_the_sheet(w, h, placement, subtitle, align):
    rows, _ = _outside("W" * 41, placement, subtitle, w=w, h=h, height=25.0, align=align)
    points = [pt for row in rows for pl in row for pt in pl]
    assert points
    for x, y in points:
        assert MARGIN - 1e-6 <= x <= w - MARGIN + 1e-6
        assert MARGIN - 1e-6 <= y <= h - MARGIN + 1e-6


def test_outside_render_time_stays_close_to_inside():
    def timed(**kw):
        samples = []
        for _ in range(5):
            t0 = time.perf_counter()
            labels.render_label(
                "Budapest - Center", height_mm=5.0, align="right", page_w=PAGE_W,
                page_h=PAGE_H, margin_mm=MARGIN, subtitle="Map 1:5000", **kw)
            samples.append(time.perf_counter() - t0)
        return statistics.median(samples)

    timed(placement="inside")  # warm the glyph caches
    inside = timed(placement="inside")
    outside = timed(placement="below")
    assert outside <= 1.2 * inside + 0.005


def _convert_svg(label, page=None):
    import io
    import xml.etree.ElementTree as ET

    from PIL import Image

    from backend.penplot.pipeline import ConvertParams, run_convert
    from backend.penplot.router import get_settings

    data = png_bytes()
    w, h = Image.open(io.BytesIO(data)).size
    body = default_params("hatch") | {"label": label} | ({"page": page} if page else {})
    res = run_convert(
        image_id="title-placement-test", image_bytes=data, is_vector=False,
        src_w=w, src_h=h, params=ConvertParams.model_validate(body),
        settings=get_settings(),
    )
    return res.svg_text, ET.fromstring(res.svg_text)


def test_convert_lines_inside_match_the_legacy_text():
    by_text, _ = _convert_svg({"enabled": True, "text": "Budapest - Map 1:5000"})
    by_lines, _ = _convert_svg({"enabled": True, "lines": ["Budapest", "Map 1:5000"]})
    assert by_lines == by_text


def test_convert_outside_sheet_carries_bounds_on_the_root_and_no_rect():
    _, root = _convert_svg(
        {"enabled": True, "lines": ["Budapest", "Map 1:5000"], "placement": "below"},
        page={"frame": True},
    )
    assert root.attrib["data-kp-a4-bounds"] == "0 0 210.000 297.000"
    assert not [el for el in root.iter() if el.attrib.get("id") == "a4-bounds"]


def test_stats_table_top_reserve_clears_an_above_title():
    from backend.penplot.stats_table import layout_stats_table

    rows = [("highways", 12), ("roads", 340)]
    kw = dict(position="top-left", page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN)
    lines, _, keepout = layout_stats_table(rows, reserve_top_mm=20.0, **kw)
    assert min(_ys(lines)) >= MARGIN + 20.0
    assert keepout is not None and keepout[1] >= MARGIN + 20.0
    free, _, _ = layout_stats_table(rows, **kw)
    assert min(_ys(free)) < MARGIN + 20.0
