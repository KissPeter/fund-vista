"""Title-block labels: Hershey geometry units + HTTP behavior."""

from __future__ import annotations

from backend.penplot import labels
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
    # Comma tail hangs below the cap baseline — anchoring uses the real bbox.
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
    params = _labeled(default_params("hatch"), align="center")
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

    # Worst case: ink everywhere — hatch fills the whole image area.
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
