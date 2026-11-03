"""Config loading and source probing/ordering (probe tests need ffmpeg)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from plotvid import config, probe

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
])
def test_invalid_configs(tmp_path, body, msg):
    (tmp_path / "a.mp4").touch()
    with pytest.raises(config.ConfigError, match=msg):
        config.load(write(tmp_path, body))


def make_clip(path: Path, created: str | None) -> None:
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=1",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"]
    if created:
        cmd += ["-metadata", f"creation_time={created}"]
    subprocess.run([*cmd, str(path)], check=True)


def test_rotation_swaps_display_size():
    # Phone portrait clips: coded 1024x576 with a -90 display matrix.
    stream = {"side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]}
    assert probe._rotation(stream) == -90
    assert probe._rotation({"tags": {"rotate": "180"}}) == 180
    assert probe._rotation({}) == 0
    info = probe.SourceInfo(Path("x"), 1.0, 1024, 576, -90, 60.0, True,
                            probe.datetime.now(probe.UTC), "container")
    assert info.display_size == (576, 1024)


@needs_ffmpeg
def test_order_by_creation_time(tmp_path):
    late, early = tmp_path / "a_late.mp4", tmp_path / "z_early.mp4"
    make_clip(late, "2026-10-30T12:00:00Z")
    make_clip(early, "2026-10-30T09:00:00Z")
    infos = probe.order_sources([late, early])
    assert [i.path.name for i in infos] == ["z_early.mp4", "a_late.mp4"]
    assert all(i.created_from == "container" for i in infos)
    assert infos[0].has_audio
    assert infos[0].display_size == (320, 180)
    assert infos[0].fps == pytest.approx(30)
