"""ZnikoSL single-stroke face: loader, default status, Hungarian coverage."""

from __future__ import annotations

from backend.penplot import labels
from backend.penplot import stats_table
from backend.penplot import svgfont


def test_face_loads_with_latin_and_accents():
    face = svgfont.get_svg_face()
    assert face["cap_height"] > 0.0
    assert face["space_advance"] > 0.0
    assert len(face["glyphs"]) > 400
    for ch in ["H", "A", "g", "0", ".", ",", "-", "/", "(", "&", "%"]:
        assert ch in face["glyphs"], ch
    for ch in ["\u00e9", "\u0151", "\u0171", "\u00e1", "\u00cd"]:
        assert ch in face["glyphs"], ch
    assert "\x01" not in face["glyphs"]
    assert " " not in face["glyphs"]  # space via space_advance, like Hershey


def test_face_advances_positive():
    face = svgfont.get_svg_face()
    for ch, g in face["glyphs"].items():
        assert g["advance"] >= 0.0, ch
    # Plotter-relevant set: printable ASCII + Hungarian accents advance.
    import string

    wanted = string.ascii_letters + string.digits + "\u00e9\u00e8\u00ea\u00eb\u00e1\u00e0\u00ed\u00ec\u00f3\u00f2\u00f6\u0151\u0150\u00fa\u00f9\u00fc\u0171\u0170\u00c9\u00c1\u00cd\u00d3\u00d6\u0150\u00da\u00dc\u0170"
    for ch in wanted:
        assert face["glyphs"][ch]["advance"] > 0.0, ch


def test_labels_default_is_zniko():
    assert labels.DEFAULT_FONT == svgfont.FONT_ID
    assert labels.LABEL_FONTS[0] == svgfont.FONT_ID
    cap, _, glyphs = labels._resolve_face(svgfont.FONT_ID, list("Hi"))
    assert cap > 0.0 and "H" in glyphs and "i" in glyphs


def test_stats_table_default_is_zniko():
    assert stats_table.FONT == svgfont.FONT_ID


def test_hungarian_title_draws_without_warnings():
    lines, warnings = labels.render_label(
        "Budapest Erzs\u00e9bet h\u00edd 04L", height_mm=5.0, align="right",
        page_w=210.0, page_h=297.0, margin_mm=10.0, border=False,
    )
    assert len(lines) > 0
    assert warnings == []


def test_unknown_font_falls_back_to_default():
    cap, _, glyphs = labels._resolve_face("no-such-face", list("A"))
    assert cap > 0.0 and "A" in glyphs
