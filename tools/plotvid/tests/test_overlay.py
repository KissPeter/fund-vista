"""The intro overlay's frame-by-frame animation: pure string maths, no video."""

from __future__ import annotations

import re
from pathlib import Path

from plotvid import overlay

COMMON = Path(__file__).resolve().parent.parent / "common"


def _frame(lang: str, t: float) -> str:
    static = overlay._static_svg((COMMON / f"intro-{lang}.svg").read_text(encoding="utf-8"))
    return overlay._frame_svg(static, t)


def test_css_animation_is_removed():
    for lang in ("hu", "en"):
        static = overlay._static_svg((COMMON / f"intro-{lang}.svg").read_text(encoding="utf-8"))
        assert "@keyframes" not in static and "animation" not in static


def test_intro_fades_in_holds_and_fades_out():
    def opacity(t: float) -> float:
        return float(re.search(r'<g id="ov" opacity="([\d.]+)"', _frame("en", t)).group(1))

    length = overlay.END_S - overlay.START_S
    assert opacity(0.0) == 0.0
    assert opacity(1.5) == 1.0
    assert 0.0 < opacity(length - 0.15) < 1.0
    assert opacity(length) == 0.0


def test_underline_draws_then_stays():
    def offset(t: float) -> float:
        return float(re.search(r'stroke-dashoffset="([\d.]+)"', _frame("hu", t)).group(1))

    assert offset(0.0) == 1.0 and offset(0.3) == 1.0
    assert 0.0 < offset(0.65) < 1.0
    assert offset(1.0) == 0.0 and offset(3.0) == 0.0


def test_intro_boxes_are_centred():
    for lang in ("hu", "en"):
        text = (COMMON / f"intro-{lang}.svg").read_text(encoding="utf-8")
        view = float(re.search(r'viewBox="0 0 (\d+) ', text).group(1))
        x = float(re.search(r'<rect x="(\d+)" [^>]*width="(\d+)"', text).group(1))
        w = float(re.search(r'<rect x="\d+" [^>]*width="(\d+)"', text).group(1))
        assert x == view - (x + w)


def test_saved_plan_replays_the_same_segments(tmp_path):
    from types import SimpleNamespace

    from plotvid import folder

    infos = [SimpleNamespace(path=tmp_path / name, display_size=(1920, 1080), rotate_filter=None,
                             has_audio=True) for name in ("a.mp4", "b.mp4")]
    plan = {"clips": [{"clip": 1, "segments": [
        {"file": "b.mp4", "start_s": 2.0, "end_s": 12.5}, {"file": "a.mp4", "start_s": 30.0, "end_s": 35.0}]}]}
    (clip,) = folder._clips_from_plan(plan, infos)
    assert [(p.path.name, p.start_s, p.duration_s) for p in clip] == [("b.mp4", 2.0, 10.5), ("a.mp4", 30.0, 5.0)]
    plan["clips"][0]["segments"][0]["file"] = "gone.mp4"
    import pytest
    with pytest.raises(RuntimeError, match="gone.mp4"):
        folder._clips_from_plan(plan, infos)
