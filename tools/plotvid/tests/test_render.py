"""End-to-end render of a hand-built plan on synthetic lavfi sources (needs ffmpeg)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plotvid import cli, manifest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def make_source(path: Path, size: str, rate: int, duration: float, audio: bool = True) -> None:
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
           "-i", f"testsrc=size={size}:rate={rate}:duration={duration}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}:sample_rate=44100",
                "-c:a", "aac"]
    subprocess.run([*cmd, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


def ffprobe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                          "-show_streams", str(path)], check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def hand_plan(tmp_path: Path, src: Path, spans: list[tuple[float, float]]) -> None:
    data = {
        "version": manifest.VERSION, "plot": "test", "clip": 1,
        "zone": {"index": 0, "start_s": 0, "end_s": 10}, "seed": 1, "relax_level": 0,
        "footage_s": sum(b - a for a, b in spans),
        "segments": [{"source": str(src), "start_s": a, "end_s": b} for a, b in spans],
        "final_photo": {"path": None, "seconds": 2.5,
                        "placeholder": {"source": str(src), "time_s": 9.0}},
    }
    manifest.write(tmp_path / "out" / "plan" / "clip_01.json", data)


def run(tmp_path: Path, src: Path, extra: str = "") -> dict:
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f"plot: Test plot\nsources: [{src.name}]\nout_dir: out\n{extra}")
    assert cli.main(["render", str(cfg)]) == 0
    return ffprobe(tmp_path / "out" / "clips" / "clip_01.mp4")


def streams(info: dict) -> tuple[dict, dict]:
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    return v, a


def test_render_cut_portrait_source(tmp_path):
    src = tmp_path / "src.mp4"
    make_source(src, "576x1024", 30, 10)
    hand_plan(tmp_path, src, [(1.0, 4.0), (5.0, 8.5)])
    info = run(tmp_path, src, "render: {transition: cut}\n")
    v, a = streams(info)
    assert (v["codec_name"], a["codec_name"]) == ("h264", "aac")
    assert (v["width"], v["height"]) == (576, 1024)  # exact 9:16, native size
    assert v["r_frame_rate"] == "30/1"  # source fps kept
    assert int(a["sample_rate"]) == 48000 and a["channels"] == 2
    # 3 + 3.5 s of footage, 2.5 s photo, 2.5 s outro card.
    assert float(info["format"]["duration"]) == pytest.approx(11.5, abs=0.15)
    assert (tmp_path / "out" / "assets" / "outro_card.png").exists()


def test_render_crossfade_wide_source_video_outro(tmp_path):
    src = tmp_path / "wide.mp4"
    make_source(src, "1280x720", 60, 10)
    outro = tmp_path / "outro.mp4"
    make_source(outro, "640x360", 25, 2)
    hand_plan(tmp_path, src, [(0.5, 4.0), (6.0, 9.0)])
    info = run(tmp_path, src, "render: {transition: crossfade, crossfade_s: 0.25}\n"
                              "outro: {path: outro.mp4, seconds: null}\n")
    v, _ = streams(info)
    assert (v["width"], v["height"]) == (404, 720)  # too wide: sides cropped, no upscale
    assert abs(v["width"] / v["height"] - 9 / 16) < 0.01
    assert v["r_frame_rate"] == "60/1"
    # 3.5 + 3 + 2.5 photo + 2 outro video, minus three 0.25 s crossfades.
    assert float(info["format"]["duration"]) == pytest.approx(11.0 - 0.75, abs=0.15)


def test_render_default_is_soft_and_matches_duration_math(tmp_path):
    from plotvid import assign
    src = tmp_path / "src.mp4"
    make_source(src, "576x1024", 30, 10)
    spans = [(0.5, 4.0), (5.0, 8.5), (8.5, 9.9)]
    hand_plan(tmp_path, src, spans)
    info = run(tmp_path, src)  # defaults: crossfade 0.5 s
    # 3 segments + photo + outro = 4 joins of 0.5 s.
    expect = assign.Budget(5.0, 0.5).total(sum(b - a for a, b in spans), 3)
    assert expect == pytest.approx(3.5 + 3.5 + 1.4 + 5.0 - 2.0)
    assert float(info["format"]["duration"]) == pytest.approx(expect, abs=0.15)
