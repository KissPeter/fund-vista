"""Technical-drawing convert options: OCR text, circles, thin lines, upscale.

Deterministic: the OCR *engine* is replaced by injected words so these tests
need no Tesseract; the engine itself is covered in test_penplot_ocr_text.py and
the HTTP test at the bottom uses whatever the host has (it only asserts the
request succeeds and degrades cleanly).
"""

from __future__ import annotations

import threading
import time

import cv2
import numpy as np
import pytest

from backend.cancel import ClientCancelled
from backend.penplot import drawing, imaging, ocr_text
from backend.penplot.config import Settings
from backend.penplot.pipeline import effective_params_dump, params_hash, run_convert
from backend.penplot.schemas import ConvertParams

# Stable since before the drawing options existed; if this changes, every
# stored result/cached convert was silently invalidated.
LEGACY_DEFAULT_HASH = "36fc02273dcf"


def _drawing_png() -> bytes:
    """White sheet, a thick 'label' blob and a ring, like a tiny dimension drawing."""
    img = np.full((200, 300), 255, np.uint8)
    cv2.rectangle(img, (100, 80), (140, 92), 0, -1)          # stands in for a label
    cv2.circle(img, (220, 100), 30, 0, 1)                    # tyre
    cv2.circle(img, (220, 100), 14, 0, 1)                    # rim
    cv2.line(img, (20, 150), (280, 150), 120, 1)             # pale dimension line
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _convert(params: ConvertParams, tmp_path, *, data=None, cancelled=None, **settings_kw):
    data = data or _drawing_png()
    w, h, _ = imaging.probe_raster(data)
    settings = Settings(data_dir=str(tmp_path), **settings_kw)
    return run_convert(
        image_id="a" * 64, image_bytes=data, is_vector=False,
        src_w=float(w), src_h=float(h), params=params, settings=settings,
        cancelled=cancelled)


def _word(text="1234") -> ocr_text.Word:
    return ocr_text.Word(text, 100.0, 80.0, 40.0, 12.0, conf=90.0)


@pytest.fixture
def fake_ocr(monkeypatch):
    """Tesseract 'present' and reading one known word; counts engine runs."""
    calls = {"n": 0}

    def read(small, *, settings, cancelled, warnings):
        calls["n"] += 1
        return [_word()]

    monkeypatch.setattr(drawing, "ocr_available", lambda *a, **k: True)
    monkeypatch.setattr(drawing, "_read_words", read)
    return calls


# ----------------------------------------------------------- params / cache key

def test_defaults_leave_cache_key_and_dump_untouched():
    assert params_hash(ConvertParams()) == LEGACY_DEFAULT_HASH
    dump = effective_params_dump(ConvertParams(), is_vector=False)
    assert not {"ocr_text", "circles", "thin_lines", "trace_upscale"} & dump.keys()


def test_enabled_options_change_the_key_and_vectors_ignore_them():
    base = params_hash(ConvertParams())
    for kw in ({"circles": True}, {"thin_lines": True}, {"trace_upscale": 2},
               {"ocr_text": {"enabled": True}}):
        assert params_hash(ConvertParams(**kw)) != base, kw
        assert params_hash(ConvertParams(**kw), is_vector=True) == params_hash(
            ConvertParams(), is_vector=True), kw
    # min_conf is meaningless while OCR is off: it must not fork the cache.
    assert params_hash(ConvertParams(ocr_text={"enabled": False, "min_conf": 50})) == base


def test_param_validation_bounds():
    for bad in ({"trace_upscale": 4}, {"trace_upscale": 0}, {"ocr_text": {"min_conf": 1}},
                {"ocr_text": {"nope": 1}}, {"ocr_text": {"min_chars": 0}}):
        with pytest.raises(ValueError):
            ConvertParams(**bad)


def test_preset_is_a_valid_param_set():
    p = ConvertParams(**drawing.DRAWING_PRESET)
    assert p.ocr_text.enabled and p.circles and p.thin_lines and p.trace_upscale == 3


# --------------------------------------------------------------------- ink mask

def test_thin_lines_keeps_pale_strokes_the_greyscale_blur_drops():
    g = np.full((60, 200), 255, np.uint8)
    g[30, 10:190] = 190  # pale 1 px line
    _, legacy, _, _ = drawing.build_ink(
        g, threshold=215, blur=1.0, thin_lines=False, upscale=1, max_pixels=10**7)
    _, thin, _, _ = drawing.build_ink(
        g, threshold=215, blur=1.0, thin_lines=True, upscale=1, max_pixels=10**7)
    assert not legacy.any() and thin.any()


def test_thin_lines_keeps_1px_strokes_at_any_blur():
    g = np.full((60, 200), 255, np.uint8)
    g[30, 10:190] = 100
    for blur in (0.3, 0.7, 1.0, 2.0, 4.0):
        for up in (1, 3):
            _, ink, _, _ = drawing.build_ink(
                g, threshold=215, blur=blur, thin_lines=True, upscale=up, max_pixels=10**8)
            assert ink.any(), (blur, up)
    assert drawing.mask_level(0.7) == drawing.MASK_SMOOTH_LEVEL  # preset unchanged


def test_upscale_is_clamped_by_total_pixels_with_a_warning():
    g = np.full((1000, 1000), 255, np.uint8)
    gu, ink, u, warns = drawing.build_ink(
        g, threshold=128, blur=0.0, thin_lines=True, upscale=3, max_pixels=4_000_000)
    assert u == 2 and gu.shape == (2000, 2000) and ink.shape == (2000, 2000)
    assert drawing.WARNING_UPSCALE_CLAMPED in warns
    _, _, u, warns = drawing.build_ink(
        g, threshold=128, blur=0.0, thin_lines=True, upscale=3, max_pixels=10**7)
    assert u == 3 and not warns


# -------------------------------------------------------------------- pipeline

def test_ocr_text_replaces_the_traced_label_and_reads_once_per_tone(tmp_path, fake_ocr):
    off = _convert(ConvertParams(methods=["centerline"], thin_lines=True), tmp_path)
    on = _convert(ConvertParams(methods=["centerline"], thin_lines=True,
                                ocr_text={"enabled": True}), tmp_path)
    assert fake_ocr["n"] == 1
    assert on.svg_text != off.svg_text
    # A slider tweak (curve_smooth) must reuse the cached words, not re-OCR.
    _convert(ConvertParams(methods=["centerline"], thin_lines=True, curve_smooth=2,
                           ocr_text={"enabled": True}), tmp_path)
    assert fake_ocr["n"] == 1
    # A different tone setting changes what the engine sees -> re-reads.
    _convert(ConvertParams(methods=["centerline"], thin_lines=True, contrast=1.5,
                           ocr_text={"enabled": True}), tmp_path)
    assert fake_ocr["n"] == 2


def test_low_confidence_and_short_words_stay_as_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(drawing, "ocr_available", lambda *a, **k: True)
    monkeypatch.setattr(drawing, "_read_words", lambda *a, **k: [
        ocr_text.Word("13", 100.0, 80.0, 40.0, 12.0, conf=90.0)])  # 2 chars: clutter
    res = _convert(ConvertParams(methods=["centerline"], ocr_text={"enabled": True}), tmp_path)
    assert ocr_text.WARNING_LOW_CONF in res.warnings


def test_missing_tesseract_degrades_to_a_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(drawing, "ocr_available", lambda *a, **k: False)
    res = _convert(ConvertParams(methods=["centerline"], ocr_text={"enabled": True}), tmp_path)
    assert drawing.WARNING_OCR_UNAVAILABLE in res.warnings and res.svg_text


def test_circles_become_exact_polylines(tmp_path):
    off = _convert(ConvertParams(methods=["centerline"], thin_lines=True), tmp_path)
    on = _convert(ConvertParams(methods=["centerline"], thin_lines=True, circles=True), tmp_path)
    assert on.svg_text != off.svg_text
    assert on.stats.points.before > off.stats.points.before  # dense exact rings added


def test_legacy_path_does_not_emit_drawing_warnings(tmp_path):
    res = _convert(ConvertParams(methods=["centerline"]), tmp_path)
    assert not [w for w in res.warnings if w.startswith(("ocr_", "trace_"))]


# -------------------------------------------- budget / concurrency / cancellation

def test_engine_stops_at_its_deadline_and_says_so(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("Tesseract must not run after the deadline")

    monkeypatch.setattr(ocr_text, "tesseract_reader", boom)
    g = np.full((60, 200), 255, np.uint8)
    cv2.putText(g, "1234", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.4, 0, 1)
    eng = ocr_text.SheetEngine(deadline=0.0)  # long in the past
    assert eng(g) == [] and eng.budget_exceeded


def test_budget_overrun_returns_no_words_and_a_warning(monkeypatch, tmp_path):
    class Slow:
        budget_exceeded = True

        def __init__(self, **kw): ...

        def __call__(self, gray):
            return [_word()]

    monkeypatch.setattr(ocr_text, "SheetEngine", Slow)
    warns: list[str] = []
    out = drawing._read_words(np.zeros((10, 10), np.uint8),
                              settings=Settings(data_dir=str(tmp_path)),
                              cancelled=None, warnings=warns)
    assert out is None and drawing.WARNING_OCR_BUDGET in warns


def test_ocr_waits_for_the_slot_then_gives_up_with_ocr_busy(tmp_path):
    settings = Settings(data_dir=str(tmp_path), ocr_max_concurrent=1, ocr_queue_s=0.4)
    fd = drawing._acquire_slot(settings, None, time.monotonic() + 1)
    assert fd is not None
    try:
        warns: list[str] = []
        assert drawing._read_words(np.zeros((8, 8), np.uint8), settings=settings,
                                   cancelled=None, warnings=warns) is None
        assert drawing.WARNING_OCR_BUSY in warns
    finally:
        drawing._release_slot(fd)
    # Released: the next caller gets the slot straight away.
    fd = drawing._acquire_slot(settings, None, time.monotonic() + 1)
    assert fd is not None
    drawing._release_slot(fd)


def test_slots_are_machine_wide_across_processes(tmp_path):
    """A second *process* must not get the slot while this one holds it."""
    import subprocess
    import sys

    settings = Settings(data_dir=str(tmp_path), ocr_max_concurrent=1)
    fd = drawing._acquire_slot(settings, None, time.monotonic() + 1)
    assert fd is not None
    try:
        code = (
            "import sys,time; from backend.penplot import drawing; "
            "from backend.penplot.config import Settings; "
            f"s=Settings(data_dir={str(tmp_path)!r}, ocr_max_concurrent=1); "
            "fd=drawing._acquire_slot(s,None,time.monotonic()+0.5); "
            "sys.exit(0 if fd is None else 7)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True)
        assert out.returncode == 0, out.stderr.decode()
    finally:
        drawing._release_slot(fd)


def test_cancel_while_queued_for_the_slot_raises(tmp_path):
    settings = Settings(data_dir=str(tmp_path), ocr_max_concurrent=1, ocr_queue_s=30)
    fd = drawing._acquire_slot(settings, None, time.monotonic() + 1)
    assert fd is not None
    try:
        flag = threading.Event()
        threading.Timer(0.3, flag.set).start()
        with pytest.raises(ClientCancelled):
            drawing._read_words(np.zeros((8, 8), np.uint8), settings=settings,
                                cancelled=flag.is_set, warnings=[])
    finally:
        drawing._release_slot(fd)


def test_alternative_ocr_backend_plugs_in_via_the_registry(tmp_path, monkeypatch):
    """A recogniser needs only the ``SheetReader`` signature; here a fake one."""
    seen = {}

    def reader(sheet, bounds, whitelist):
        seen["rows"] = len(bounds)
        return ["1234"] * len(bounds)

    monkeypatch.setitem(drawing.OCR_BACKENDS, "fake", (lambda: True, lambda: reader))
    settings = Settings(data_dir=str(tmp_path), ocr_backend="fake")
    assert drawing.ocr_available(settings)
    img = np.full((60, 200), 255, np.uint8)
    cv2.putText(img, "1234", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 0, 1)
    words = drawing.ocr_text.SheetEngine(reader=reader, workers=1)(img)
    assert seen["rows"] >= 1 and all(w.text == "1234" for w in words)
    assert not drawing.ocr_available(Settings(data_dir=str(tmp_path), ocr_backend="nope"))


def test_second_heavy_trace_falls_back_to_1x_instead_of_stacking_memory(tmp_path):
    settings = Settings(data_dir=str(tmp_path), trace_max_concurrent=1, ocr_queue_s=0.3)
    with drawing.trace_slot(settings) as first:
        assert first is True
        res = _convert(ConvertParams(methods=["centerline"], thin_lines=True,
                                     trace_upscale=3), tmp_path, ocr_queue_s=0.3)
    assert drawing.WARNING_TRACE_BUSY in res.warnings
    # Free again: a normal run gets the slot and no warning.
    res2 = _convert(ConvertParams(methods=["centerline"], thin_lines=True,
                                  trace_upscale=2), tmp_path)
    assert drawing.WARNING_TRACE_BUSY not in res2.warnings


def test_slot_is_released_when_a_convert_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(drawing, "build_ink", lambda *a, **k: 1 / 0)
    with pytest.raises(Exception):
        _convert(ConvertParams(methods=["centerline"], thin_lines=True), tmp_path)
    settings = Settings(data_dir=str(tmp_path))
    with drawing.trace_slot(settings) as ok:
        assert ok is True


def test_large_images_are_ocrd_at_capped_size_and_mapped_back(tmp_path, monkeypatch):
    seen = {}

    def read(small, *, settings, cancelled, warnings):
        seen["shape"] = small.shape
        return [ocr_text.Word("1234", 10.0, 20.0, 40.0, 12.0, conf=90.0)]

    monkeypatch.setattr(drawing, "ocr_available", lambda *a, **k: True)
    monkeypatch.setattr(drawing, "_read_words", read)
    tone = np.full((800, 1200), 255, np.uint8)
    out = drawing.analyse(
        tone, ocr=ConvertParams(ocr_text={"enabled": True}).ocr_text, circles=False,
        settings=Settings(data_dir=str(tmp_path), ocr_max_dim_px=600),
        image_id="b" * 64, tone_args=(1.0, 0.0, False))
    assert seen["shape"] == (400, 600)  # halved
    xs = [x for line in out.text_px for x, _ in line]
    # Word box x 10..50 on the small image -> 20..100 at full size.
    assert min(xs) >= 20 - 1 and max(xs) <= 100 + 1


# -------------------------------------------------------------------- over HTTP

def test_convert_endpoint_accepts_the_options_and_rejects_bad_ones(http_client):
    data = _drawing_png()
    up = http_client.post("/v1/images", files={"file": ("d.png", data, "image/png")})
    assert up.status_code == 200, up.text
    image_id = up.json()["image_id"]
    ok = http_client.post("/v1/convert", json={
        "image_id": image_id, "params": drawing.DRAWING_PRESET})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["svg_url"] and body["stats"]["strokes"] > 0
    bad = http_client.post("/v1/convert", json={
        "image_id": image_id, "params": {"trace_upscale": 9}})
    assert bad.status_code == 422
    bad2 = http_client.post("/v1/convert", json={
        "image_id": image_id, "params": {"ocr_text": {"enabled": True, "bogus": 1}}})
    assert bad2.status_code == 422
