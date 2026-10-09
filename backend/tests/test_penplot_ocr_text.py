"""Spike tests for the OCR -> single-stroke text stage (no OCR engine needed)."""

import cv2
import numpy as np
import pytest

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


def test_cli_builds_the_preset_params():
    from backend.penplot import drawing, img2plot
    from backend.penplot.schemas import ConvertParams

    args = img2plot.make_parser().parse_args(["x.png"])
    assert img2plot.build_params(args) == ConvertParams(**drawing.DRAWING_PRESET)
    args = img2plot.make_parser().parse_args(
        ["x.png", "--upscale", "2", "--no-ocr", "--keep-frame", "--frame",
         "--frame-radius-mm", "6", "--label", "PLAN 1"])
    tweaked = img2plot.build_params(args)
    assert tweaked.trace_upscale == 2 and not tweaked.ocr_text.enabled
    assert tweaked.strip_frame is False and tweaked.page.frame and tweaked.page.frame_radius_mm == 6
    assert tweaked.label.enabled and tweaked.label.text == "PLAN 1"


def test_cli_plans_outputs_and_never_overwrites_same_stems(tmp_path):
    from backend.penplot import img2plot

    (tmp_path / "scans").mkdir()
    for name in ("b.png", "a.JPG", "notes.txt"):
        (tmp_path / "scans" / name).write_bytes(b"x")
    srcs = img2plot.expand_sources([str(tmp_path / "scans"), "https://h.example/dir/a.jpg"])
    assert [s.rsplit("/", 1)[-1] for s in srcs] == ["a.JPG", "b.png", "a.jpg"]  # folder sorted, txt ignored
    plan = img2plot.plan_outputs(srcs, str(tmp_path / "out"))
    outs = [o for _, o in plan]
    assert len(set(outs)) == 3 and outs[0].endswith("a.svg") and outs[1].endswith("b.svg")
    assert outs[2].rsplit("/", 1)[-1].startswith("a-") and outs[2].endswith(".svg")


def test_cli_batch_survives_a_bad_file_and_reports(tmp_path):
    import json

    from backend.penplot import img2plot

    good = tmp_path / "good.png"
    img = np.full((120, 160), 255, np.uint8)
    cv2.rectangle(img, (30, 30), (120, 90), 0, 2)
    cv2.imwrite(str(good), img)
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    rc = img2plot.main([str(good), str(bad), "--out-dir", str(tmp_path / "o"),
                        "--report", str(tmp_path / "r.json"), "--no-ocr", "-q"])
    assert rc == img2plot.EXIT_FAILED
    rep = json.loads((tmp_path / "r.json").read_text())
    assert rep["summary"]["ok"] == 1 and rep["summary"]["failed"] == 1
    by = {r["src"].rsplit("/", 1)[-1]: r for r in rep["results"]}
    assert by["good.png"]["status"] == "ok" and by["good.png"]["strokes"] > 0
    assert by["bad.png"]["status"] == "error" and "readable" in by["bad.png"]["error"]
    assert (tmp_path / "o" / "good.svg").exists() and not (tmp_path / "o" / "bad.svg").exists()
    assert not list((tmp_path / "o").glob("*.tmp"))  # atomic writes leave no temp files
    # Rerun: the good one is skipped (output newer than input), the bad one retried.
    rc = img2plot.main([str(good), str(bad), "--out-dir", str(tmp_path / "o"),
                        "--skip-existing", "--no-ocr", "-q"])
    assert rc == img2plot.EXIT_FAILED
    assert img2plot.main([str(good), "--out-dir", str(tmp_path / "o"),
                          "--skip-existing", "--no-ocr", "-q"]) == img2plot.EXIT_OK


def test_cli_usage_errors_exit_2(tmp_path, capsys):
    from backend.penplot import img2plot

    with pytest.raises(SystemExit) as exc:
        img2plot.main(["a.png", "b.png"])  # several inputs, no --out-dir
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        img2plot.main(["a.png", "--upscale", "9", "--out-dir", str(tmp_path)])  # bad value
    assert exc.value.code == 2  # clean usage error, not a traceback


def test_strip_lines_exact_keeps_glyph_row_touching_the_line():
    """A number sitting on its dimension line must not lose the row touching it."""
    img = np.full((60, 120), 255, np.uint8)
    img[40, 10:110] = 0            # dimension line, 1 px thick
    img[33:40, 50:53] = 0          # a glyph stem ending right above the line
    wide = ocr_text.strip_lines(img)
    exact = ocr_text.strip_lines(img, grow=0)
    assert (wide[39, 50:53] == 255).all()   # dilated removal ate the stem's last row
    assert (exact[39, 50:53] == 0).all()    # exact removal keeps it
    assert (exact[40, 20:40] == 255).all()  # the line itself is still gone


def _w(text, conf):
    return ocr_text.Word(text, 0.0, 0.0, 20.0, 8.0, conf=conf)


def test_cut_off_reading_never_beats_the_longer_one():
    """A clipped "197" must not replace "1975", whatever its confidence."""
    assert not ocr_text._replaces(_w("197", 90), [_w("1975", 40)])
    assert ocr_text._replaces(_w("1975", 30), [_w("197", 90)])
    assert not ocr_text._replaces(_w("1975", 10), [_w("197", 90)])  # too unsure
    assert ocr_text._replaces(_w("7170", 70), [_w("1170", 40)])      # plain confidence
    assert not ocr_text._replaces(_w("7170", 40), [_w("1170", 70)])
