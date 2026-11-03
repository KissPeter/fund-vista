"""YAML config -> frozen dataclasses, validated strictly (unknown keys fail)."""

from __future__ import annotations

import glob
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class DetectConfig:
    # 48 kHz: the recordings' native rate; the tuned detector was validated at it.
    sample_rate: int = 48000
    # STFT hop/window. 5 ms resolves dense detail trains (~0.17 s apart) and
    # later the 80-100 ms pen-up pause.
    hop_s: float = 0.005
    window_s: float = 0.02133  # 1024 samples at 48 kHz
    # Servo band. The servo burst is strongest above 8 kHz; stepper tones and
    # direction changes live below (a 2.5-14 kHz band fired on them).
    band_lo_hz: float = 8000.0
    band_hi_hz: float = 15000.0
    # Running median over the band power; strips 1-2 frame spikes only.
    tick_s: float = 0.025
    # Moving average that shapes a burst into one hump.
    smooth_s: float = 0.04
    # Baseline: this percentile of the smoothed level within +-baseline_s.
    baseline_s: float = 0.5
    baseline_percentile: float = 20.0
    min_prominence_db: float = 6.0
    min_gap_s: float = 0.1
    # Burst width: contiguous time within width_drop_db of the peak, measured
    # at ~1 ms resolution. Gate (None = off): events narrower than
    # min_width_s are reported as dropped. Servo bursts measure 13-100 ms;
    # the vid2 false tick at 48.62 s about 10 ms.
    width_drop_db: float = 6.0
    min_width_s: float | None = 0.012  # accepted on vid2: drops both known false ticks
    ignore_before_s: float = 0.0
    ignore_after_s: float | None = None


@dataclass(frozen=True)
class InkConfig:
    """Video ink check (see ink.py). Pixel thresholds are in 0..255 gray."""
    enabled: bool = True
    fps: float = 10.0
    width: int = 288  # analysis width in px; height follows the display aspect
    # Optional paper region override, fractions of the displayed frame:
    # [x0, y0, x1, y1]. null = every bright, unoccluded pixel counts.
    roi: tuple[float, float, float, float] | None = None
    ink_threshold: int = 25  # black top-hat response that counts as a pen line
    paper_level: int = 150  # smoothed brightness above which a pixel is paper
    occluder_ratio: float = 0.75  # blob darker than this x local paper = arm/pen
    persist_s: float = 0.7  # new ink must stay visible this long after the span
    persist_frac: float = 0.75  # ... in at least this share of frames seeing it


@dataclass(frozen=True)
class StrokesConfig:
    # Stroke = interval between consecutive pen events. Long = duration at or
    # above this percentile of all stroke durations in the source.
    long_percentile: float = 70.0
    # A stroke is drawing when at least this much new ink (px at 288 px
    # analysis width) appears over it.
    drawn_min_px: float = 20.0
    # If fewer than this share of long strokes read as drawing, the ink check
    # is deemed unreliable and every stroke is labelled unknown (= any event
    # may cut).
    min_drawn_share: float = 0.1


@dataclass(frozen=True)
class SegmentConfig:
    min_s: float = 3.0
    max_s: float = 8.0
    # Segments start this many short strokes before a long stroke.
    lead_short: tuple[int, ...] = (2, 3)
    pre_roll: float = 0.15  # start this long before the first event
    post_roll: float = 0.25  # end this long after the closing pen event
    # Relaxation levels tried until every clip can reach its minimum length:
    # 0 strict, 1 any lead-in before a long stroke, 2 end after any drawn
    # stroke / start at any event, 3 every event is a cut candidate.
    max_relax: int = 3
    relax_penalty: float = 0.5  # score multiplier per relaxation level
    # The plot's very last segment may run to the end of the last source and
    # exceed max_s by up to this much, so the pen's trip home is not cut off.
    final_extra_s: float = 3.0


@dataclass(frozen=True)
class VideosConfig:
    # Number of clips; "auto" = the most (up to max_count) that can each reach
    # min_total_s without reusing footage.
    count: int | str = "auto"
    max_count: int = 8
    # Story parts: the plotting timeline is cut into this many equal time
    # ranges and every clip takes segments from every one, in order.
    parts: int = 3
    # Whole-clip lengths: plotting footage + final photo + outro, minus the
    # crossfade overlaps.
    target_s: float = 35.0
    min_total_s: float = 30.0
    max_total_s: float = 40.0
    seed: int = 42
    attempts: int = 24  # seeded draws per (count, relaxation level); best fit wins


@dataclass(frozen=True)
class RenderConfig:
    fps: float | None = None  # None = first source's frame rate
    crf: int = 20
    preset: str = "medium"
    audio_bitrate: str = "160k"
    audio_fade_s: float = 0.02  # fade in/out at every segment cut
    transition: str = "crossfade"  # "cut" | "crossfade"
    crossfade_s: float = 0.5
    crossfade_type: str = "fade"  # an xfade transition: fade | dissolve | fadeblack | fadewhite


@dataclass(frozen=True)
class FinalPhotoConfig:
    path: Path | None = None  # None = last clean frame of the last source
    seconds: float = 2.5


@dataclass(frozen=True)
class OutroConfig:
    path: Path | None = None  # image or video; None = generated card
    seconds: float | None = 2.5  # video: None = its full length
    text: str = "drawn by a pen plotter"


@dataclass(frozen=True)
class Config:
    plot: str
    sources: tuple[Path, ...]
    out_dir: Path
    detect: DetectConfig = field(default_factory=DetectConfig)
    ink: InkConfig = field(default_factory=InkConfig)
    strokes: StrokesConfig = field(default_factory=StrokesConfig)
    segment: SegmentConfig = field(default_factory=SegmentConfig)
    videos: VideosConfig = field(default_factory=VideosConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    final_photo: FinalPhotoConfig = field(default_factory=FinalPhotoConfig)
    outro: OutroConfig = field(default_factory=OutroConfig)


def _build(cls, raw: dict[str, Any] | None, section: str):
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{section}: expected a mapping")
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(f"{section}: unknown keys {unknown}")
    return cls(**raw)


def _resolve_sources(patterns: list[str], base: Path) -> tuple[Path, ...]:
    found: list[Path] = []
    for pat in patterns:
        p = Path(pat).expanduser()
        if not p.is_absolute():
            p = base / p
        matches = sorted(Path(m) for m in glob.glob(str(p)))
        if not matches:
            raise ConfigError(f"sources: nothing matches {pat!r}")
        found.extend(m.resolve() for m in matches)
    # Order of first appearance; a file matched by two globs is used once.
    return tuple(dict.fromkeys(found))


CROSSFADE_TYPES = ("fade", "dissolve", "fadeblack", "fadewhite")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def _asset(value: Any, base: Path, section: str) -> Path | None:
    if value is None:
        return None
    p = Path(str(value)).expanduser()
    p = p if p.is_absolute() else base / p
    if not p.is_file():
        raise ConfigError(f"{section}: path {value!r} not found")
    return p.resolve()


def load(path: str | Path) -> Config:
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    base = path.parent

    allowed = {"plot", "sources", "out_dir", "detect", "ink", "strokes", "segment", "videos",
               "render", "final_photo", "outro"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unknown top-level keys {unknown}")

    plot = raw.get("plot")
    if not plot or not isinstance(plot, str):
        raise ConfigError("plot: required string")
    sources = raw.get("sources")
    if isinstance(sources, str):
        sources = [sources]
    if not sources:
        raise ConfigError("sources: required (path or glob list)")

    detect = _build(DetectConfig, raw.get("detect"), "detect")
    if min(detect.min_gap_s, detect.hop_s, detect.smooth_s, detect.baseline_s) <= 0:
        raise ConfigError("detect: min_gap_s, hop_s, smooth_s and baseline_s must be > 0")
    if detect.tick_s < 0:
        raise ConfigError("detect: tick_s must be >= 0")
    if detect.window_s < detect.hop_s:
        raise ConfigError("detect: window_s must be >= hop_s")
    if detect.width_drop_db <= 0:
        raise ConfigError("detect: width_drop_db must be > 0")
    if detect.min_width_s is not None and detect.min_width_s < 0:
        raise ConfigError("detect: min_width_s must be >= 0 or null")
    if not 0 <= detect.baseline_percentile <= 100:
        raise ConfigError("detect: baseline_percentile must be within 0..100")
    if not 0 < detect.band_lo_hz < detect.band_hi_hz <= detect.sample_rate / 2:
        raise ConfigError("detect: need 0 < band_lo_hz < band_hi_hz <= Nyquist")

    ink = _build(InkConfig, raw.get("ink"), "ink")
    if ink.roi is not None:
        roi = tuple(float(v) for v in ink.roi)
        if len(roi) != 4 or not (0 <= roi[0] < roi[2] <= 1 and 0 <= roi[1] < roi[3] <= 1):
            raise ConfigError("ink: roi must be [x0, y0, x1, y1] fractions with x0<x1, y0<y1")
        ink = replace(ink, roi=roi)
    if ink.fps <= 0 or ink.width < 64:
        raise ConfigError("ink: fps must be > 0 and width >= 64")
    if not 0 < ink.persist_frac <= 1 or ink.persist_s <= 0:
        raise ConfigError("ink: need persist_s > 0 and 0 < persist_frac <= 1")

    strokes = _build(StrokesConfig, raw.get("strokes"), "strokes")
    if not 0 <= strokes.long_percentile <= 100:
        raise ConfigError("strokes: long_percentile must be within 0..100")
    if strokes.drawn_min_px < 0 or not 0 <= strokes.min_drawn_share <= 1:
        raise ConfigError("strokes: drawn_min_px >= 0 and min_drawn_share within 0..1")

    segment = _build(SegmentConfig, raw.get("segment"), "segment")
    lead = segment.lead_short
    lead = (lead,) if isinstance(lead, int) else tuple(int(v) for v in lead)
    segment = replace(segment, lead_short=lead)
    if not 0 < segment.min_s <= segment.max_s:
        raise ConfigError("segment: need 0 < min_s <= max_s")
    if not lead or min(lead) < 0:
        raise ConfigError("segment: lead_short must be non-negative integers")
    if segment.pre_roll < 0 or segment.post_roll < 0:
        raise ConfigError("segment: pre_roll and post_roll must be >= 0")
    if not 0 <= segment.max_relax <= 3 or not 0 < segment.relax_penalty <= 1:
        raise ConfigError("segment: max_relax within 0..3, relax_penalty within (0, 1]")
    if segment.final_extra_s < 0:
        raise ConfigError("segment: final_extra_s must be >= 0")

    videos = _build(VideosConfig, raw.get("videos"), "videos")
    if not (videos.count == "auto" or (isinstance(videos.count, int) and videos.count >= 1)):
        raise ConfigError("videos: count must be a positive integer or 'auto'")
    if videos.max_count < 1 or videos.attempts < 1 or videos.parts < 1:
        raise ConfigError("videos: max_count, attempts and parts must be >= 1")
    if not 0 < videos.min_total_s <= videos.target_s <= videos.max_total_s:
        raise ConfigError("videos: need 0 < min_total_s <= target_s <= max_total_s")

    render = _build(RenderConfig, raw.get("render"), "render")
    if render.transition not in ("cut", "crossfade"):
        raise ConfigError("render: transition must be 'cut' or 'crossfade'")
    if render.fps is not None and render.fps <= 0:
        raise ConfigError("render: fps must be > 0 or null")
    if render.crossfade_s <= 0 or render.audio_fade_s < 0:
        raise ConfigError("render: crossfade_s > 0 and audio_fade_s >= 0")
    if render.crossfade_type not in CROSSFADE_TYPES:
        raise ConfigError(f"render: crossfade_type must be one of {list(CROSSFADE_TYPES)}")
    if render.transition == "crossfade" and render.crossfade_s * 2 >= segment.min_s:
        raise ConfigError("render: crossfade_s must be under half of segment.min_s")

    final_photo = _build(FinalPhotoConfig, raw.get("final_photo"), "final_photo")
    outro = _build(OutroConfig, raw.get("outro"), "outro")
    final_photo = replace(final_photo, path=_asset(final_photo.path, base, "final_photo"))
    outro = replace(outro, path=_asset(outro.path, base, "outro"))
    if final_photo.seconds <= 0:
        raise ConfigError("final_photo: seconds must be > 0")
    if outro.seconds is not None and outro.seconds <= 0:
        raise ConfigError("outro: seconds must be > 0 or null")
    if render.transition == "crossfade" and min(
            final_photo.seconds, outro.seconds or float("inf")) <= render.crossfade_s:
        raise ConfigError("render: crossfade_s must be shorter than final_photo.seconds "
                          "and outro.seconds")
    if outro.seconds is None and (outro.path is None or outro.path.suffix.lower() in IMAGE_EXTS):
        raise ConfigError("outro: seconds may be null only for a video")

    out_dir = Path(raw.get("out_dir", "out")).expanduser()
    if not out_dir.is_absolute():
        out_dir = base / out_dir

    return Config(
        plot=plot,
        sources=_resolve_sources(list(sources), base),
        out_dir=out_dir,
        detect=detect,
        ink=ink,
        strokes=strokes,
        segment=segment,
        videos=videos,
        render=render,
        final_photo=final_photo,
        outro=outro,
    )
