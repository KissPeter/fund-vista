"""YAML config -> frozen dataclasses.

Only the sections implemented so far are validated strictly; sections owned
by later pipeline steps (strokes, segment, videos, render, final_photo,
outro) are accepted and kept raw in ``Config.extra`` until their step lands.
"""

from __future__ import annotations

import glob
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

FUTURE_SECTIONS = {"strokes", "segment", "videos", "render", "final_photo", "outro"}


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
    # at ~1 ms resolution. Optional gate (None = off): events narrower than
    # min_width_s are reported as dropped. Servo bursts measure 35-100 ms;
    # the vid2 false tick at 48.62 s about 10 ms.
    width_drop_db: float = 6.0
    min_width_s: float | None = None
    ignore_before_s: float = 0.0
    ignore_after_s: float | None = None


@dataclass(frozen=True)
class Config:
    plot: str
    sources: tuple[Path, ...]
    out_dir: Path
    detect: DetectConfig = field(default_factory=DetectConfig)
    extra: dict[str, Any] = field(default_factory=dict)


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


def load(path: str | Path) -> Config:
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    base = path.parent

    allowed = {"plot", "sources", "out_dir", "detect"} | FUTURE_SECTIONS
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

    out_dir = Path(raw.get("out_dir", "out")).expanduser()
    if not out_dir.is_absolute():
        out_dir = base / out_dir

    return Config(
        plot=plot,
        sources=_resolve_sources(list(sources), base),
        out_dir=out_dir,
        detect=detect,
        extra={k: raw[k] for k in FUTURE_SECTIONS if k in raw},
    )
