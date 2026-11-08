"""Config loading and source probing/ordering (probe tests need ffmpeg)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from plotvid import config, ink, probe, render

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "cfg.yaml"
    p.write_text(text)
    return p


def test_load_defaults_and_relative_paths(tmp_path):
    (tmp_path / "v").mkdir()
    (tmp_path / "v" / "b.mp4").touch()
    (tmp_path / "v" / "a.mp4").touch()
    cfg = config.load(write(tmp_path, "plot: p\nsources: ['v/*.mp4', 'v/a.mp4']\n"))
    assert [s.name for s in cfg.sources] == ["a.mp4", "b.mp4"]
    assert cfg.out_dir == tmp_path / "out"
    assert cfg.detect == config.DetectConfig()


def test_clip_sections_parsed(tmp_path):
    (tmp_path / "a.mp4").touch()
    (tmp_path / "photo.jpg").touch()
    cfg = config.load(write(tmp_path, (
        "plot: p\nsources: a.mp4\nvideos: {count: 3, seed: 7, parts: 4}\n"
        "segment: {lead_short: [1, 2]}\nink: {roi: [0, 0.1, 1, 0.9]}\n"
        "render: {transition: crossfade, crossfade_s: 0.3}\n"
        "final_photo: {path: photo.jpg, seconds: 2}\n")))
    assert cfg.videos.count == 3 and cfg.videos.seed == 7
    assert cfg.videos.target_s == 35.0 and cfg.videos.parts == 4 and cfg.videos.count != "auto"
    assert cfg.segment.lead_short == (1, 2)
    assert cfg.ink.roi == (0.0, 0.1, 1.0, 0.9)
    assert cfg.render.transition == "crossfade" and cfg.render.crossfade_type == "fade"
    assert cfg.final_photo.path == (tmp_path / "photo.jpg").resolve()
    assert cfg.outro.path is None and cfg.outro.seconds == 2.5


@pytest.mark.parametrize("body, msg", [
    ("sources: a.mp4\n", "plot"),
    ("plot: p\n", "sources"),
    ("plot: p\nsources: missing*.mp4\n", "nothing matches"),
    ("plot: p\nsources: a.mp4\ndetect: {k_mad: 3, bogus: 1}\n", "unknown keys"),
    ("plot: p\nsources: a.mp4\nwhatever: 1\n", "unknown top-level"),
    ("plot: p\nsources: a.mp4\ndetect: {band_hi_hz: 30000}\n", "Nyquist"),
    ("plot: p\nsources: a.mp4\nrender: {transition: wipe}\n", "transition"),
    ("plot: p\nsources: a.mp4\nvideos: {count: 0}\n", "count"),
    ("plot: p\nsources: a.mp4\nvideos: {min_total_s: 41}\n", "min_total_s"),
    ("plot: p\nsources: a.mp4\nvideos: {parts: 0}\n", "parts"),
    ("plot: p\nsources: a.mp4\nrender: {crossfade_type: spin}\n", "crossfade_type"),
    ("plot: p\nsources: a.mp4\nfinal_photo: {seconds: 0.4}\n", "shorter than"),
    ("plot: p\nsources: a.mp4\nsegment: {min_s: 9}\n", "min_s"),
    ("plot: p\nsources: a.mp4\nink: {roi: [0.5, 0, 0.2, 1]}\n", "roi"),
    ("plot: p\nsources: a.mp4\noutro: {path: nope.mp4}\n", "not found"),
    ("plot: p\nsources: a.mp4\noutro: {seconds: null}\n", "only for a video"),
    ("plot: p\nsources: a.mp4\nstrokes: {split_piece_s: 0}\n", "split_piece_s"),
    ("plot: p\nsources: a.mp4\nstrokes: {split_over_s: 5}\n", "split_over_s"),
    ("plot: p\nsources: a.mp4\nrender: {rotate: 45}\n", "render: rotate must be"),
    ("plot: p\nsources: a.mp4\nrender: {rotate: sideways}\n", "render: rotate must be"),
    ("plot: p\nsources: a.mp4\nrender: {rotate: -90}\n", "render: rotate must be"),
    ("plot: p\nsources: a.mp4\nrender: {watermark: nope.png}\n", "not found"),
    ("plot: p\nsources: a.mp4\nrender: {watermark_width_frac: 0}\n", "within"),
    ("plot: p\nsources: a.mp4\nrender: {watermark_width_frac: 0.6}\n", "within"),
    ("plot: p\nsources: a.mp4\nrender: {watermark_margin_frac: -0.1}\n", "within"),
])
def test_invalid_configs(tmp_path, body, msg):
    (tmp_path / "a.mp4").touch()
    with pytest.raises(config.ConfigError, match=msg):
        config.load(write(tmp_path, body))


def test_strokes_split_defaults_and_null(tmp_path):
    (tmp_path / "a.mp4").touch()
    cfg = config.load(write(tmp_path, "plot: p\nsources: a.mp4\n"))
    assert cfg.strokes.split_over_s == 20.0 and cfg.strokes.split_piece_s == 7.0
    cfg = config.load(write(tmp_path, "plot: p\nsources: a.mp4\nstrokes: {split_over_s: null}\n"))
    assert cfg.strokes.split_over_s is None


def make_clip(path: Path, created: str | None, size: str = "320x180",
              display_rotation: int | None = None) -> None:
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30:duration=1",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"]
    if created:
        cmd += ["-metadata", f"creation_time={created}"]
    if display_rotation is None:
        subprocess.run([*cmd, str(path)], check=True)
        return
    # Remux with a Display Matrix: ffmpeg takes display_rotation as an input option.
    plain = path.with_name(f"plain_{path.name}")
    subprocess.run([*cmd, str(plain)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation:v:0", str(display_rotation),
                    "-i", str(plain), "-c", "copy", str(path)], check=True)


# The -display_rotation input option appeared in FFmpeg 6.0; the CI runner ships 5.1.
needs_display_rotation = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or "display_rotation" not in subprocess.run(
        ["ffmpeg", "-hide_banner", "-h", "full"], capture_output=True, text=True).stdout,
    reason="ffmpeg lacks the -display_rotation option (needs FFmpeg 6.0+)")


def test_rotation_swaps_display_size():
    # Phone portrait clips: coded 1024x576 with a -90 display matrix.
    stream = {"side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]}
    assert probe._rotation(stream) == -90
    assert probe._rotation({"tags": {"rotate": "180"}}) == 180
    assert probe._rotation({}) == 0
    info = probe.SourceInfo(Path("x"), 1.0, 1024, 576, -90, 60.0, True,
                            probe.datetime.now(probe.UTC), "container")
    assert info.display_size == (576, 1024)


def test_render_rotate_default_and_values(tmp_path):
    (tmp_path / "a.mp4").touch()
    assert config.load(write(tmp_path, "plot: p\nsources: a.mp4\n")).render.rotate == "auto"
    for raw in ("auto", "0", "90", "180", "270"):
        cfg = config.load(write(tmp_path, f"plot: p\nsources: a.mp4\nrender: {{rotate: {raw}}}\n"))
        assert cfg.render.rotate == raw


def test_render_watermark_defaults_and_relative_path(tmp_path):
    (tmp_path / "a.mp4").touch()
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "wm.svg").touch()
    cfg = config.load(write(tmp_path, "plot: p\nsources: a.mp4\n"))
    assert cfg.render.watermark is None
    assert cfg.render.watermark_width_frac == 0.2 and cfg.render.watermark_margin_frac == 0.04
    cfg = config.load(write(tmp_path, (
        "plot: p\nsources: a.mp4\nrender: {watermark: assets/wm.svg, watermark_width_frac: 0.25,"
        " watermark_margin_frac: 0.05}\n")))
    assert cfg.render.watermark == (tmp_path / "assets" / "wm.svg").resolve()
    assert cfg.render.watermark_width_frac == 0.25 and cfg.render.watermark_margin_frac == 0.05
    cfg = config.load(write(tmp_path, "plot: p\nsources: a.mp4\nrender: {watermark: null}\n"))
    assert cfg.render.watermark is None


def test_folder_script_resolves_watermark_against_yaml_folder(tmp_path):
    from plotvid import folder
    (tmp_path / "cfg" / "assets").mkdir(parents=True)
    (tmp_path / "cfg" / "assets" / "wm.svg").touch()
    yaml_path = tmp_path / "cfg" / "folder.yaml"
    yaml_path.write_text("plot: p\nrender: {watermark: assets/wm.svg}\n")
    work = tmp_path / "work"
    work.mkdir()
    cfg = folder._load_config(yaml_path, [], work)  # the copy in work/ would resolve it wrongly
    assert cfg.render.watermark == (tmp_path / "cfg" / "assets" / "wm.svg").resolve()


@pytest.mark.parametrize("setting, tagged, stored, expect", [
    ("auto", False, (1920, 1080), 270),  # untagged landscape: camera turned to portrait
    ("auto", False, (1080, 1920), 0),  # untagged portrait: already upright
    ("auto", True, (1920, 1080), 0),  # tagged: ffmpeg autorotate already applied it
    ("auto", True, (1080, 1920), 0),
    ("0", False, (1920, 1080), 0),  # explicit values win over auto
    ("90", False, (1920, 1080), 90),
    ("180", True, (1080, 1920), 180),
    ("270", False, (1080, 1920), 270),
])
def test_resolve_rotation(setting, tagged, stored, expect):
    assert probe.resolve_rotation(setting, tagged, *stored) == expect


def test_extra_rotation_swaps_display_size_and_sets_filter():
    now = probe.datetime.now(probe.UTC)
    info = probe.SourceInfo(Path("x"), 1.0, 1920, 1080, 0, 60.0, True, now, "mtime",
                            extra_rotation=270)
    assert info.display_size == (1080, 1920)
    assert info.rotate_filter == "transpose=2"
    # A tag of -90 plus 270 more is 180 in all: the display stays landscape.
    tagged = probe.SourceInfo(Path("x"), 1.0, 1920, 1080, -90, 60.0, True, now, "mtime",
                              extra_rotation=270)
    assert tagged.display_size == (1920, 1080)
    assert tagged.rotate_filter == "transpose=2"
    filters = {0: "", 90: "transpose=1", 180: "hflip,vflip", 270: "transpose=2"}
    for extra, text in filters.items():
        info = probe.SourceInfo(Path("x"), 1.0, 1920, 1080, 0, 60.0, True, now, "mtime",
                                extra_rotation=extra)
        assert info.rotate_filter == text


@needs_ffmpeg
def test_probe_auto_rotation_only_for_untagged_landscape(tmp_path):
    landscape, portrait = tmp_path / "land.mp4", tmp_path / "port.mp4"
    make_clip(landscape, None, "320x180")
    make_clip(portrait, None, "180x320")
    info = probe.probe(landscape)
    assert info.extra_rotation == 270 and info.display_size == (180, 320)
    assert info.rotate_filter == "transpose=2"
    info = probe.probe(portrait)
    assert info.extra_rotation == 0 and info.display_size == (180, 320)
    assert probe.probe(landscape, "0").extra_rotation == 0
    assert probe.probe(landscape, "0").display_size == (320, 180)
    assert probe.probe(portrait, "90").extra_rotation == 90
    assert probe.order_sources([landscape], "270")[0].display_size == (180, 320)


@needs_ffmpeg
@needs_display_rotation
def test_probe_tagged_display_matrix_is_not_auto_rotated(tmp_path):
    tagged = tmp_path / "tag.mp4"
    make_clip(tagged, None, "320x180", display_rotation=-90)
    info = probe.probe(tagged)
    assert info.rotation == -90 and info.extra_rotation == 0 and info.display_size == (180, 320)


def test_output_size_for_rotated_landscape_source_needs_no_crop():
    assert render.crop_9_16(1080, 1920) == (1080, 1920, 0, 0)
    assert render.output_size([(1080, 1920)]) == (1080, 1920)
    # Without the rotation the same file would lose most of its width.
    assert render.output_size([(1920, 1080)]) == (606, 1080)


@needs_ffmpeg
def test_decode_gray_applies_rotation_before_scaling(tmp_path):
    # Dark on the left, bright on the right; 90 degrees counter-clockwise puts
    # the bright edge at the top.
    src = tmp_path / "ramp.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:s=320x180:r=10:d=1,geq=lum='X*255/320':cb=128:cr=128",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    frames = ink.decode_gray(src, 10.0, (180, 320), rotate="transpose=2")
    assert frames.shape[1:] == (320, 180)
    assert frames[0][:80].mean() > frames[0][-80:].mean() + 50


@needs_ffmpeg
def test_order_by_creation_time(tmp_path):
    late, early = tmp_path / "a_late.mp4", tmp_path / "z_early.mp4"
    make_clip(late, "2026-10-30T12:00:00Z")
    make_clip(early, "2026-10-30T09:00:00Z")
    infos = probe.order_sources([late, early], "0")
    assert [i.path.name for i in infos] == ["z_early.mp4", "a_late.mp4"]
    assert all(i.created_from == "container" for i in infos)
    assert infos[0].has_audio
    assert infos[0].display_size == (320, 180)
    # Default auto turns this untagged landscape clip to portrait.
    assert probe.order_sources([late, early])[0].display_size == (180, 320)
    assert infos[0].fps == pytest.approx(30)
