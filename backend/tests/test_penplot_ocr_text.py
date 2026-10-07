"""Spike tests for the OCR -> single-stroke text stage (no OCR engine needed)."""

import numpy as np

from backend.penplot import ocr_text, svgfont


def test_layout_text_is_stroke_polylines_in_face_space():
    lines, skipped, width = ocr_text.layout_text("H412")
    assert not skipped and width > 0
    assert len(lines) >= 4 and all(len(p) >= 2 for p in lines)
    cap = svgfont.get_svg_face()["cap_height"]
    ys = [y for p in lines for _, y in p]
    assert min(ys) >= -1 and max(ys) <= cap + 1  # digits sit on the cap band


def test_layout_text_spaces_and_unknown_chars():
    _, skipped, w_ab = ocr_text.layout_text("A\u4e2dB")
    assert skipped == ["\u4e2d"]
    _, _, w_spaced = ocr_text.layout_text("A B")
    _, _, w_tight = ocr_text.layout_text("AB")
    assert w_spaced > w_tight  # space advances without drawing


def test_word_polylines_fit_box_and_rotate():
    w = ocr_text.Word("OK", x=100, y=50, w=40, h=20, conf=90)
    lines, _ = ocr_text.word_to_polylines(w)
    xs = [x for p in lines for x, _ in p]
    ys = [y for p in lines for _, y in p]
    assert 99.5 <= min(xs) and max(xs) <= 140.5  # round glyphs overshoot ~0.1px
    assert 49.5 <= min(ys) and max(ys) <= 70.5
    r = ocr_text.Word("OK", x=100, y=50, w=40, h=20, conf=90, angle=90)
    rl, _ = ocr_text.word_to_polylines(r)
    rx = [x for p in rl for x, _ in p]
    ry = [y for p in rl for _, y in p]
    assert (max(ry) - min(ry)) > (max(rx) - min(rx))  # now taller than wide


def test_prepare_masks_confident_words_only():
    gray = np.full((100, 200), 255, np.uint8)
    gray[10:30, 10:60] = 0   # confident word
    gray[60:80, 10:60] = 0   # low-confidence word: must survive
    words = [
        ocr_text.Word("412", 10, 10, 50, 20, conf=95),
        ocr_text.Word("???", 10, 60, 50, 20, conf=20),
    ]
    cleaned, lines, warns = ocr_text.prepare(gray, lambda g: words)
    assert cleaned[10:30, 10:60].min() == 255
    assert cleaned[60:80, 10:60].max() == 0
    assert lines and ocr_text.WARNING_LOW_CONF in warns


def test_text_candidates_find_horizontal_and_rotated_labels():
    import cv2

    img = np.full((120, 200), 255, np.uint8)
    cv2.putText(img, "2130", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.45, 0, 1)
    tmp = np.full((30, 60), 255, np.uint8)
    cv2.putText(tmp, "350", (2, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, 0, 1)
    img[40:100, 150:180] = cv2.rotate(tmp, cv2.ROTATE_90_COUNTERCLOCKWISE)
    cands = ocr_text._text_candidates(img)
    assert any(c[4] == "h" and c[1] < 40 for c in cands)
    assert any(c[4] == "v" and c[0] >= 140 for c in cands)


def test_overlap_is_fraction_of_smaller_box():
    assert ocr_text._overlap((0, 0, 10, 10), (0, 0, 5, 5)) == 1.0
    assert ocr_text._overlap((0, 0, 10, 10), (20, 20, 5, 5)) == 0.0


def test_detect_circles_finds_concentric_rings_and_ignores_corners():
    import cv2

    from backend.penplot import arcs

    img = np.full((160, 200), 255, np.uint8)
    cv2.circle(img, (60, 80), 30, 0, 1)   # tyre
    cv2.circle(img, (60, 80), 15, 0, 1)   # rim
    cv2.rectangle(img, (120, 40), (190, 120), 0, 1)  # not a circle
    rings = arcs.detect_circles(img)
    near = [(round(r)) for cx, cy, r in rings if abs(cx - 60) < 2 and abs(cy - 80) < 2]
    assert 30 in near and 15 in near
    assert all(abs(cx - 60) < 3 for cx, cy, _ in rings)
    cleaned = arcs.mask_circles(img, rings)
    assert cleaned[80, 90] == 255  # tyre ink removed
    line = arcs.circle_polyline(60, 80, 30)
    assert line[0] == line[-1]


def test_find_repeats_copies_a_read_word_onto_its_twin():
    import cv2

    img = np.full((80, 200), 255, np.uint8)
    for ox, oy in ((20, 10), (120, 50)):
        cv2.putText(img, "7o", (ox, oy + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 0, 1)
    read = [ocr_text.Word("7\u00b0", 18, 10, 18, 16, conf=90)]
    twins = ocr_text.find_repeats(img, read)
    assert len(twins) == 1 and abs(twins[0].x - 118) <= 1 and abs(twins[0].y - 50) <= 1
    assert twins[0].text == "7\u00b0"


def test_cli_builds_the_preset_params(monkeypatch):
    from backend.penplot import drawing, img2plot
    from backend.penplot.schemas import ConvertParams

    ap_args = img2plot.argparse.Namespace(
        threshold=None, contrast=None, brightness=None, blur=None, prune_px=None,
        simplify=None, upscale=None, curve_smooth=None, linemerge_mm=None,
        linesimplify_mm=None, min_conf=None, min_chars=None, size=None,
        orientation=None, margin_mm=None, no_ocr=False, no_circles=False)
    params = img2plot.build_params(ap_args)
    assert params == ConvertParams(**drawing.DRAWING_PRESET)
    ap_args.upscale, ap_args.no_ocr = 2, True
    tweaked = img2plot.build_params(ap_args)
    assert tweaked.trace_upscale == 2 and not tweaked.ocr_text.enabled
