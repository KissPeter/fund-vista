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
        border=False,
    )
    assert len(lines) > 0  # the A still draws
    assert any(w.startswith("label_unsupported_characters") for w in warnings)


def test_border_draws_title_block_strip():
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
    assert len(boxed) == len(plain) + 2
    divider, frame = boxed[-2], boxed[-1]
    # Divider spans the full inner width, joining the frame verticals.
    assert len(divider) == 2
    assert abs(divider[0][0] - MARGIN) < 1e-6
    assert abs(divider[1][0] - (PAGE_W - MARGIN)) < 1e-6
    assert abs(divider[0][1] - divider[1][1]) < 1e-9
    # Frame is the closed margin rect.
    assert len(frame) == 5 and frame[0] == frame[-1]
    assert abs(frame[0][0] - MARGIN) < 1e-6
    assert abs(frame[1][0] - (PAGE_W - MARGIN)) < 1e-6
    # Divider sits above the text, below the page top; text in the strip.
    px0, py0, px1, py1 = _bbox(plain)
    assert divider[0][1] < py0
    assert divider[0][1] > MARGIN
    bx0, by0, bx1, by1 = _bbox(boxed)
    assert bx0 == MARGIN and bx1 == PAGE_W - MARGIN
    assert by1 == PAGE_H - MARGIN


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


def test_fill_spans_padded_slot_and_frame_stays_full_width():
    lines, _ = labels.render_label(
        "AB", height_mm=5.0, align="fill",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=True, border_radius_mm=0.0,
        pad_left_mm=5.0, pad_right_mm=7.0,
    )
    divider, frame = lines[-2], lines[-1]
    # Frame/divider still span the full inner width.
    assert abs(frame[0][0] - MARGIN) < 1e-6
    assert abs(frame[1][0] - (PAGE_W - MARGIN)) < 1e-6
    assert abs(divider[0][0] - MARGIN) < 1e-6
    assert abs(divider[1][0] - (PAGE_W - MARGIN)) < 1e-6
    # But the text itself stays inside the padded slot.
    text_lines = lines[:-2]
    x0, _, x1, _ = _bbox(text_lines)
    assert x0 >= MARGIN + 5.0 - 1e-6
    assert x1 <= PAGE_W - MARGIN - 7.0 + 1e-6


def test_rounded_frame_defaults_and_clamps():
    _, _ = labels.render_label(
        "AB", height_mm=5.0, align="right",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=True, border_radius_mm=0.0,
    )
    boxed, _ = labels.render_label(
        "AB", height_mm=5.0, align="right",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=True,  # default radius 2.0
    )
    frame = boxed[-1]
    assert len(frame) == 2 + 4 * 8 + 1  # straights + arc chords + close
    assert frame[0] == frame[-1]
    assert abs(frame[0][0] - (MARGIN + 2.0)) < 1e-6
    assert abs(frame[0][1] - MARGIN) < 1e-6
    for x, y in frame:
        assert MARGIN - 1e-6 <= x <= PAGE_W - MARGIN + 1e-6
        assert MARGIN - 1e-6 <= y <= PAGE_H - MARGIN + 1e-6
    # Absurd radius clamps to half the frame's smaller side, never inverts.
    huge, _ = labels.render_label(
        "AB", height_mm=5.0, align="right",
        page_w=PAGE_W, page_h=PAGE_H, margin_mm=MARGIN,
        border=True, border_radius_mm=500.0,
    )
    hframe = huge[-1]
    assert hframe[0] == hframe[-1]
    for x, y in hframe:
        assert MARGIN - 1e-6 <= x <= PAGE_W - MARGIN + 1e-6
        assert MARGIN - 1e-6 <= y <= PAGE_H - MARGIN + 1e-6


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
    assert boxed["stats"]["strokes"] == open_box["stats"]["strokes"] + 2


def test_label_fonts_differ_over_http(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    urls = set()
    for font in ("futural", "futuram", "simplex"):
        body = _convert(
            http_client, image_id, _labeled(default_params("hatch"), font=font)
        ).json()
        urls.add(body["svg_url"])
    assert len(urls) == 3


def test_label_invalid_font_422(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = _labeled(default_params("hatch"), font="comic-sans")
    resp = _convert(http_client, image_id, params)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_params"


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
