"""End-to-end render of a hand-built plan on synthetic lavfi sources (needs ffmpeg)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from plotvid import cli, manifest, render
from plotvid.config import RenderConfig

REPO = Path(__file__).resolve().parents[1]

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
    info = run(tmp_path, src, "render: {transition: crossfade, crossfade_s: 0.25, rotate: 0}\n"
                              "outro: {path: outro.mp4, seconds: null}\n")
    v, _ = streams(info)
    assert (v["width"], v["height"]) == (404, 720)  # too wide: sides cropped, no upscale
    assert abs(v["width"] / v["height"] - 9 / 16) < 0.01
    assert v["r_frame_rate"] == "60/1"
    # 3.5 + 3 + 2.5 photo + 2 outro video, minus three 0.25 s crossfades.
    assert float(info["format"]["duration"]) == pytest.approx(11.0 - 0.75, abs=0.15)


def test_render_rotated_untagged_landscape_fills_portrait_frame(tmp_path):
    # Camera turned to portrait, file stored 320x180 with no rotation tag.
    src = tmp_path / "src.mp4"
    make_source(src, "320x180", 30, 10)
    hand_plan(tmp_path, src, [(1.0, 4.0), (5.0, 8.5)])
    info = run(tmp_path, src, "render: {transition: cut}\n")
    v, _ = streams(info)
    assert (v["width"], v["height"]) == (180, 320)  # full height, no crop, no upscale
    assert float(info["format"]["duration"]) == pytest.approx(11.5, abs=0.15)


def test_segment_filter_rotates_before_crop():
    part = render.Part("segment", Path("src.mp4"), 3.0, start_s=1.0, display_size=(1080, 1920),
                       rotate="transpose=2")
    cmd = render.build_command([part], (1080, 1920), 30.0, RenderConfig(), Path("out.mp4"))
    filters = cmd[cmd.index("-filter_complex") + 1]
    assert filters.startswith("[0:v:0]transpose=2,crop=1080:1920:0:0,fps=30.0")


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


def test_build_command_without_watermark():
    part = render.Part("segment", Path("src.mp4"), 3.0, start_s=1.0, display_size=(1080, 1920))
    cmd = render.build_command([part], (1080, 1920), 30.0, RenderConfig(), Path("out.mp4"))
    filters = cmd[cmd.index("-filter_complex") + 1]
    assert cmd.count("-i") == 1 and "overlay" not in filters
    assert cmd[cmd.index("-map"):cmd.index("-map") + 4] == ["-map", "[vout]", "-map", "[aout]"]


@pytest.mark.parametrize("transition", ["cut", "crossfade"])
def test_build_command_with_watermark(transition):
    parts = [render.Part("segment", Path("a.mp4"), 3.0, start_s=1.0, display_size=(1080, 1920)),
             render.Part("segment", Path("b.mp4"), 2.5, start_s=0.0, display_size=(1080, 1920))]
    wm = render.Watermark(Path("wm.png"), (216, 230), visible_s=3.0)
    cmd = render.build_command(parts, (1080, 1920), 30.0, RenderConfig(transition=transition),
                               Path("out.mp4"), watermark=wm)
    filters = cmd[cmd.index("-filter_complex") + 1]
    # One extra looped PNG input after the two segments: input index 2.
    i = cmd.index("wm.png")
    assert cmd[i - 1] == "-i" and cmd[i - 7:i - 5] == ["-loop", "1"]
    assert "[2:v:0]format=rgba[wm]" in filters
    margin = round(1080 * 0.04)  # 43 px to the right and bottom edges
    assert (f"[vout_pre][wm]overlay=x=W-w-{margin}:y=H-h-{margin}:enable='lt(t,3.000)'"
            f":format=auto:shortest=1[vout]") in filters
    assert filters.count("[vout]") == 1 and filters.count("[vout_pre]") == 2
    assert cmd[cmd.index("-map"):cmd.index("-map") + 4] == ["-map", "[vout]", "-map", "[aout]"]


def half_red_png(path: Path) -> Path:
    """100 x 100 RGBA watermark: left half transparent, right half opaque red."""
    img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    img.paste((255, 0, 0, 255), (50, 0, 100, 100))
    img.save(path)
    return path


def near(px: tuple[int, int, int], rgb: tuple[int, int, int], tol: int = 60) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(px, rgb))


def test_watermark_in_bottom_right_corner_until_visible_s(tmp_path):
    blue = tmp_path / "blue.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=blue:size=576x1024:rate=30:duration=3", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(blue)], check=True)
    black = tmp_path / "black.png"
    Image.new("RGB", (576, 1024), (0, 0, 0)).save(black)
    parts = [render.Part("segment", blue, 3.0, start_s=0.0, display_size=(576, 1024),
                         has_audio=False),
             render.Part("image", black, 2.0, has_audio=False)]
    out = tmp_path / "out.mp4"
    wm = render.Watermark(half_red_png(tmp_path / "wm.png"), (100, 100), visible_s=1.5)
    render.render(parts, (576, 1024), 30.0, RenderConfig(transition="cut"), out, watermark=wm)

    # Box at x 453..553, y 901..1001 (margin 23 px). Left sample sits over the
    # transparent half, right sample over the red half.
    def corner(t: float) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        frame = tmp_path / f"frame_{t}.png"
        render.extract_frame(out, t, frame)
        img = Image.open(frame).convert("RGB")
        return img.getpixel((478, 951)), img.getpixel((528, 951))

    blue_rgb, red_rgb, black_rgb = (0, 0, 255), (255, 0, 0), (0, 0, 0)
    left, right = corner(0.5)  # shown
    assert near(left, blue_rgb) and near(right, red_rgb)  # alpha kept
    left, right = corner(2.0)  # footage after visible_s: no watermark
    assert near(left, blue_rgb) and near(right, blue_rgb)
    left, right = corner(3.6)  # outro: not watermarked
    assert near(left, black_rgb) and near(right, black_rgb)


@pytest.mark.skipif(shutil.which("rsvg-convert") is None, reason="rsvg-convert not on PATH")
def test_real_watermark_svg_becomes_transparent_png(tmp_path):
    png = tmp_path / "wm.png"
    w, h = render.watermark_image(REPO / "assets" / "watermark.svg", 120, png)
    img = Image.open(png)
    assert img.mode == "RGBA" and img.size == (w, h) and w == 120
    alpha = img.getchannel("A")
    assert all(alpha.getpixel(c) == 0 for c in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)])
    assert alpha.getextrema()[1] > 0  # the logo and text strokes are opaque somewhere
