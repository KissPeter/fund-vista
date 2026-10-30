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
    # 32 kHz keeps the click band below Nyquist.
    sample_rate: int = 32000
    # STFT hop/window for the click envelope. 5 ms resolves the ~15 ms servo
    # click and later the 80-100 ms pen-up pause.
    hop_s: float = 0.005
    window_s: float = 0.016
    # Band carrying the servo click. Measured on vid1: clicks stand ~20 dB
    # over plotting noise at 4-16 kHz but only ~12 dB at 2-4 kHz, where the
    # steppers are loud. Below click_lo_hz is the motor band.
    click_lo_hz: float = 4000.0
    click_hi_hz: float = 14000.0
    # Global gate: median + max(k_mad * MAD, min_rise_db) of the dB envelope,
    # per file. On vid1 the MAD is ~4 dB because quiet set-up and busy
    # plotting share one file, so k_mad stays low and the local gate below
    # does most of the work.
    k_mad: float = 3.0
    min_rise_db: float = 8.0
    # Local gate: a click is a transient, so it must stand this far above the
    # median of the envelope within +-prominence_window_s. Robust to the
    # level change between idle and plotting.
    min_prominence_db: float = 10.0
    prominence_window_s: float = 0.25
    min_gap_s: float = 0.15
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
    if detect.k_mad <= 0 or detect.min_gap_s <= 0 or detect.hop_s <= 0:
        raise ConfigError("detect: k_mad, min_gap_s and hop_s must be > 0")
    if detect.window_s < detect.hop_s:
        raise ConfigError("detect: window_s must be >= hop_s")
    if not 0 < detect.click_lo_hz < detect.click_hi_hz <= detect.sample_rate / 2:
        raise ConfigError("detect: need 0 < click_lo_hz < click_hi_hz <= Nyquist")

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
