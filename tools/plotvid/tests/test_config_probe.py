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


def test_future_sections_kept_raw(tmp_path):
    (tmp_path / "a.mp4").touch()
    cfg = config.load(write(tmp_path, "plot: p\nsources: a.mp4\nvideos: {count: 3}\n"))
    assert cfg.extra == {"videos": {"count": 3}}


@pytest.mark.parametrize("body, msg", [
    ("sources: a.mp4\n", "plot"),
    ("plot: p\n", "sources"),
    ("plot: p\nsources: missing*.mp4\n", "nothing matches"),
    ("plot: p\nsources: a.mp4\ndetect: {k_mad: 3, bogus: 1}\n", "unknown keys"),
    ("plot: p\nsources: a.mp4\nwhatever: 1\n", "unknown top-level"),
    ("plot: p\nsources: a.mp4\ndetect: {click_hi_hz: 20000}\n", "Nyquist"),
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
