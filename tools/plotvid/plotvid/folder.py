"""Turn one folder of pen-plotter footage and photos into one MP4.

``python -m plotvid.folder FOLDER [--config YAML] [--slide-s S]``

- Videos: the plotvid steps (detect, ink check, segments, story parts) pick
  one clip's footage from all videos, ordered by creation time. Each file
  keeps its own start and end: its stages begin and end with the file.
- Images: found in FOLDER and shown as a slideshow, ``--slide-s`` seconds each
  (default 3), ordered by EXIF capture time (file mtime when missing).
- Intro: images in FOLDER/``intro`` (if it exists), shown as a slideshow of
  ``INTRO_SLIDE_S`` seconds each with the same settings, ordered the same way.
- Outro: the YAML ``outro`` image or video, relative to the YAML file.
- Config: the bundled ``folder.yaml`` unless ``--config`` is given.

Order in the MP4: intro, footage, slideshow, outro. Output goes to the same path
with the ``raw`` folder replaced by ``processed`` (``.../raw/lhbp`` ->
``.../processed/lhbp/lhbp_NN.mp4``); earlier clips of that name are replaced.
Temporary files (cache, stills, slideshow) go to ``$TMPDIR/plotvid/<folder>/``.
The YAML's ``final_photo`` is not used, because the slideshow replaces it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import yaml
from PIL import Image, ImageOps

from plotvid import assign, cli, config, events, probe, render

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "folder.yaml"
VIDEO_EXTS = {".mov", ".mp4", ".m4v"}
EXIF_DATETIME = 306
EXIF_DATETIME_ORIGINAL = 36867
EXIF_IFD = 0x8769
EXIF_ORIENTATION = 274
ROTATED = {5, 6, 7, 8}  # orientations that swap width and height
PHOTO_ONLY_SIZE = (1080, 1920)
INTRO_SLIDE_S = 2.0  # seconds per intro image, whatever --slide-s says


def output_dir(folder: Path) -> Path:
    """The folder with its last ``raw`` component replaced by ``processed``;
    without one, ``processed/<name>`` next to the folder."""
    parts = list(folder.parts)
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].lower() == "raw":
            parts[i] = "processed"
            return Path(*parts)
    return folder.parent / "processed" / folder.name


def work_dir(folder: Path) -> Path:
    return Path(tempfile.gettempdir()) / "plotvid" / folder.name


def _media(folder: Path) -> tuple[list[Path], list[Path]]:
    videos, images = [], []
    for p in sorted(folder.iterdir()):
        # Skip hidden files, including macOS AppleDouble "._" companions.
        if not p.is_file() or p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if ext in VIDEO_EXTS:
            videos.append(p)
        elif ext in config.IMAGE_EXTS:
            images.append(p)
    return videos, images


def _image_info(path: Path) -> tuple[dt.datetime, tuple[int, int]]:
    with Image.open(path) as img:
        w, h = img.size
        exif = img.getexif()
        orientation = exif.get(EXIF_ORIENTATION, 1)
        raw = exif.get_ifd(EXIF_IFD).get(EXIF_DATETIME_ORIGINAL) or exif.get(EXIF_DATETIME)
    taken = None
    if raw:
        try:
            taken = dt.datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
        except ValueError:
            taken = None
    if taken is None:
        taken = dt.datetime.fromtimestamp(path.stat().st_mtime)
    return taken, ((h, w) if orientation in ROTATED else (w, h))


def _load_config(cfg_path: Path, videos: list[Path], work: Path) -> config.Config:
    """The generic YAML with this folder's sources and work dir, validated by plotvid."""
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    raw.pop("final_photo", None)
    # plotvid's loader wants a matching source; a folder without videos has none,
    # so the YAML file stands in and is cleared below.
    raw["sources"] = [str(p) for p in videos] or [str(cfg_path)]
    raw["out_dir"] = str(work)
    outro = dict(raw.get("outro") or {})
    if outro.get("path"):  # relative to the YAML, so resolve before the copy moves
        outro["path"] = str((cfg_path.parent / outro["path"]).resolve())
    raw["outro"] = outro
    render_raw = dict(raw.get("render") or {})
    if render_raw.get("watermark"):  # relative to the YAML too, resolved before the copy
        render_raw["watermark"] = str((cfg_path.parent / render_raw["watermark"]).resolve())
    raw["render"] = render_raw
    tmp = work / "plotvid.yaml"
    tmp.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    cfg = config.load(tmp)
    return cfg if videos else replace(cfg, sources=())


def _svg_to_png(svg: Path, size: tuple[int, int], png: Path, work: Path) -> None:
    """Render with librsvg. ImageMagick's own SVG renderer ignores the stylesheet
    and draws text in the wrong font. librsvg 2.48 needs two fixes first: it reads
    neither the `font:` shorthand nor the SVG 2 `href` on <use>."""
    if shutil.which("rsvg-convert") is None:
        raise RuntimeError("rsvg-convert not found (apt install librsvg2-bin); needed for the SVG outro")
    text = svg.read_text(encoding="utf-8")
    text = re.sub(r'font:\s*700\s+60px\s+[^;}]+', "font-family:Arial,sans-serif;font-size:60px;font-weight:700", text)
    text = re.sub(r'(?<![:\w-])href="', 'xlink:href="', text)
    if "xmlns:xlink" not in text:
        text = text.replace("<svg ", '<svg xmlns:xlink="http://www.w3.org/1999/xlink" ', 1)
    fixed = work / "outro_fixed.svg"
    fixed.write_text(text, encoding="utf-8")
    w, h = size
    subprocess.run(["rsvg-convert", "-w", str(w), "-h", str(h), "-f", "png", "-o", str(png),
                    str(fixed)], check=True)


def _outro(cfg: config.Config, size: tuple[int, int], work: Path) -> Path:
    path = cfg.outro.path
    if path is None:
        card = work / "outro_card.png"
        render.outro_card(size, cfg.plot, cfg.outro.text, card)
        return card
    if path.suffix.lower() == ".svg":
        png = work / "outro.png"
        _svg_to_png(path, size, png, work)
        return png
    return path


def _still(path: Path, size: tuple[int, int], out: Path, rotate: str) -> Path:
    """Full-size photo turned as the videos are (``render.rotate``), then cropped to
    the output frame. ffmpeg re-decodes a still on every frame of its 3 s hold, so
    a 4000+ px photo is shrunk once here."""
    with Image.open(path) as img:
        tagged = EXIF_ORIENTATION in img.getexif()
        img = ImageOps.exif_transpose(img)
        extra = probe.resolve_rotation(rotate, tagged, *img.size)
        img = img.rotate(-extra, expand=True).convert("RGB")  # PIL turns counter-clockwise
        img = ImageOps.fit(img, size, method=Image.Resampling.LANCZOS)
        img.save(out, quality=95)
    return out


def _shots(images: list[Path]) -> list[tuple[dt.datetime, tuple[int, int], Path]]:
    return sorted(((taken, size, p) for p in images
                   for taken, size in [_image_info(p)]), key=lambda t: (t[0], t[2].name))


def _slideshow(shots: list[tuple[dt.datetime, tuple[int, int], Path]], size: tuple[int, int],
               fps: float, cfg: config.Config, slide_s: float, work: Path,
               label: str) -> render.Part | None:
    """The images as one silent video of ``slide_s`` seconds each; None without images."""
    if not shots:
        return None
    stills = work / f"{label}_stills"
    stills.mkdir(exist_ok=True)
    slides = [render.Part(kind="image", path=_still(p, size, stills / f"{i:03d}.jpg", cfg.render.rotate),
                          duration_s=slide_s, has_audio=False)
              for i, (_, _, p) in enumerate(shots)]
    video = work / f"{label}.mp4"
    render.render(slides, size, fps, replace(cfg.render, transition="cut"), video)
    return render.asset_part(video, None)


def _footage_clips(cfg: config.Config, infos: list[probe.SourceInfo],
                   outro_s: float, overrides: dict[str, float]) -> list[list[render.Part]]:
    # One worker per source: the ink check is serial within a source, so
    # separate videos are the unit that runs in parallel.
    # A per-file --detect-prominence overrides the detector's threshold for that file.
    cfgs = [replace(cfg, detect=replace(cfg.detect, min_prominence_db=overrides[info.path.name]))
            if info.path.name in overrides else cfg for info in infos]
    workers = max(1, min(len(infos), os.cpu_count() or 1))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        analysed = list(pool.map(cli.analyse_source, cfgs, infos, [False] * len(infos)))
    per_source = []
    for info, data in zip(infos, analysed):
        labels = events.build_strokes(data["events"], data["new_ink"], cfg.strokes,
                                      data["synthetic"])
        per_source.append((labels.strokes, info.duration_s))
        print(f"{info.path.name}: {len(labels.strokes)} strokes, "
              f"ink reliable: {labels.ink_reliable}")
    tls = assign.timelines(per_source)
    crossfade = cfg.render.transition == "crossfade"
    # Each clip is footage + outro, so the length window counts the outro too.
    budget = assign.Budget(ending_s=outro_s, join_s=cfg.render.crossfade_s if crossfade else 0.0)
    result = assign.assign(tls, cfg.segment, cfg.videos, budget)
    if not result.clips:
        raise RuntimeError(f"no clip count reaches videos.min_total_s="
                           f"{cfg.videos.min_total_s} s (tried {result.tried}); adjust the YAML")
    print(f"{result.count} clips (relaxation level {result.level}); tried {result.tried}; "
          f"part pools {[round(x, 1) for x in result.pool_s]} s")
    clips = []
    for clip in result.clips:
        print(f"clip {clip.clip + 1}: {clip.footage_s:.1f} s footage, "
              f"{len(clip.segments)} segments")
        clips.append([render.Part(kind="segment", path=infos[s.source].path,
                                  duration_s=s.duration_s, start_s=s.start_s,
                                  display_size=infos[s.source].display_size,
                                  rotate=infos[s.source].rotate_filter,
                                  has_audio=infos[s.source].has_audio, fade_audio=True)
                      for s in clip.segments])
    return clips


def process(folder: Path, cfg_path: Path, slide_s: float,
            overrides: dict[str, float]) -> Path:
    folder = folder.resolve()
    name = folder.name
    out_root = output_dir(folder)
    work = work_dir(folder)
    work.mkdir(parents=True, exist_ok=True)
    out_root.mkdir(parents=True, exist_ok=True)

    videos, images = _media(folder)
    if not videos and not images:
        raise RuntimeError(f"{folder}: no videos or images")
    cfg = _load_config(cfg_path, videos, work)
    infos = probe.order_sources(videos, cfg.render.rotate) if videos else []
    shots = _shots(images)
    intro_dir = folder / "intro"
    intro_shots = _shots(_media(intro_dir)[1]) if intro_dir.is_dir() else []

    # Video sets the frame like plotvid does; a photo-only folder gets a 9:16 phone frame.
    size = render.output_size([i.display_size for i in infos]) if infos else PHOTO_ONLY_SIZE
    fps = cfg.render.fps or (render.nice_fps(infos[0].fps) if infos else 30.0)
    outro = _outro(cfg, size, work)

    outro_part = render.asset_part(outro, cfg.outro.seconds)
    wm_png, wm_size = work / "watermark.png", None
    if cfg.render.watermark is not None:
        wm_size = render.watermark_image(cfg.render.watermark,
                                         round(size[0] * cfg.render.watermark_width_frac), wm_png)
    # The length window counts the intro, slideshow and outro too, since every clip has them.
    ending_s = (len(intro_shots) * INTRO_SLIDE_S + outro_part.duration_s
                + len(shots) * slide_s)
    clips = _footage_clips(cfg, infos, ending_s, overrides) if infos else [[]]
    intro_part = _slideshow(intro_shots, size, fps, cfg, INTRO_SLIDE_S, work, "intro")
    slide_part = _slideshow(shots, size, fps, cfg, slide_s, work, "slideshow")

    # Every clip is the intro, then footage, then the slideshow, then the outro.
    # Earlier clips of this name are replaced, so a shorter run leaves no stale ones.
    for old in out_root.glob(f"{name}_[0-9][0-9].mp4"):
        old.unlink()
    (out_root / f"{name}.mp4").unlink(missing_ok=True)
    outs = []
    for i, footage in enumerate(clips):
        parts = [part for part in (intro_part, *footage, slide_part) if part is not None]
        parts.append(outro_part)
        out = out_root / (f"{name}.mp4" if len(clips) == 1 else f"{name}_{i + 1:02d}.mp4")
        # The logo runs over the whole clip except the outro, which carries its own.
        wm = None if wm_size is None else render.Watermark(
            wm_png, wm_size, render.expected_duration(parts, cfg.render) - outro_part.duration_s)
        render.render(parts, size, fps, cfg.render, out, watermark=wm)
        duration = round(render.expected_duration(parts, cfg.render), 2)
        outs.append({"output": str(out), "duration_s": duration})
        print(f"{out.name}: {duration} s")

    summary = {
        "folder": str(folder),
        "videos": [str(i.path) for i in infos],
        "images": [{"file": str(p), "taken": t.isoformat()} for t, _, p in shots],
        "intro": [{"file": str(p), "taken": t.isoformat()} for t, _, p in intro_shots],
        "size": list(size), "fps": fps, "slide_s": slide_s, "outputs": outs,
    }
    (work / "summary.json").write_text(json.dumps(summary, indent=2))
    return out_root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="plotvid.folder", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path, help="folder with videos and images")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                    help="plotvid YAML (default: the bundled folder.yaml)")
    ap.add_argument("--slide-s", type=float, default=3.0, help="seconds per image")
    ap.add_argument("--detect-prominence", action="append", default=[], metavar="FILE=DB",
                    help="pen-event threshold for one video file, e.g. BDMV.mov=3")
    args = ap.parse_args(argv)
    overrides = {}
    for item in args.detect_prominence:
        name, _, db = item.partition("=")
        if not db:
            ap.error(f"--detect-prominence needs FILE=DB, got {item!r}")
        overrides[name] = float(db)

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            print(f"plotvid.folder: {tool} not found on PATH", file=sys.stderr)
            return 2
    try:
        out = process(args.folder, args.config, args.slide_s, overrides)
    except (RuntimeError, config.ConfigError, OSError, ValueError) as exc:
        print(f"plotvid.folder: {exc}", file=sys.stderr)
        return 1
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
