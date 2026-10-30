"""Pen-servo event detection from the audio track.

A pen lift or drop sounds like a short (~40-150 ms) burst of broadband
noise that is strongest above 8 kHz. Stepper motors are tonal and mostly
lower; a direction change shows up as a tone change below 8 kHz with no
energy above.

Pipeline, on a 5 ms hop:
1. STFT power in ``band_lo_hz``..``band_hi_hz``.
2. Running median over ``tick_s``. With the 21 ms STFT window a sharp tick
   already spans ~4 frames, so this only strips 1-2 frame spikes; a loud
   tick in the band can still become an event (known limitation).
3. Moving average over ``smooth_s`` shapes each burst into one hump; to dB.
4. Baseline: running ``baseline_percentile`` over +-``baseline_s``. A low
   percentile keeps dense trains of small details (a burst every ~0.17 s)
   standing above their own baseline, where a median would not.
5. Local maxima at least ``min_prominence_db`` above the baseline, at least
   ``min_gap_s`` apart (stronger wins).

Tuned on vid2 (no-plunger pen) against hand labels. The low band
(``lf_db``) is kept for the planned pause-length labeller.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from plotvid.config import DetectConfig

_EPS = 1e-12
_CHUNK_FRAMES = 16384  # bounds STFT and rolling-window memory on long recordings


@dataclass(frozen=True)
class Envelope:
    hop_s: float
    band_db: np.ndarray  # de-ticked, smoothed servo-band level, dB per frame
    baseline_db: np.ndarray  # rolling low-percentile of band_db
    lf_db: np.ndarray  # raw level below band_lo_hz (motor band), dB per frame

    def times(self) -> np.ndarray:
        return (np.arange(len(self.band_db)) + 0.5) * self.hop_s


@dataclass(frozen=True)
class Event:
    time_s: float
    level_db: float
    prominence_db: float  # above the rolling baseline


@dataclass(frozen=True)
class Detection:
    envelope: Envelope
    min_prominence_db: float
    events: list[Event]


def _band_power(x: np.ndarray, cfg: DetectConfig) -> tuple[np.ndarray, np.ndarray, float]:
    sr = cfg.sample_rate
    hop = max(1, round(cfg.hop_s * sr))
    win = max(hop, round(cfg.window_s * sr))
    n_fft = 1 << (win - 1).bit_length()
    n_frames = max(0, 1 + (len(x) - win) // hop)
    band = np.zeros(n_frames)
    low = np.zeros(n_frames)
    if n_frames == 0:
        return band, low, hop / sr

    window = np.hanning(win).astype(np.float32)
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    in_band = (freqs >= cfg.band_lo_hz) & (freqs < cfg.band_hi_hz)
    # Skip DC/sub-sonic rumble (handling, HVAC) in the motor band.
    in_low = (freqs >= 50) & (freqs < cfg.band_lo_hz)
    norm = float((window**2).sum())
    for start in range(0, n_frames, _CHUNK_FRAMES):
        stop = min(n_frames, start + _CHUNK_FRAMES)
        idx = (np.arange(start, stop)[:, None] * hop) + np.arange(win)[None, :]
        spec = np.abs(np.fft.rfft(x[idx] * window, n=n_fft, axis=1)) ** 2
        band[start:stop] = spec[:, in_band].sum(1) / norm
        low[start:stop] = spec[:, in_low].sum(1) / norm
    return band, low, hop / sr


def _rolling(a: np.ndarray, half: int, fn) -> np.ndarray:
    """``fn(window, axis=1)`` over a centred window of 2*half+1, edge-padded, chunked."""
    if half <= 0 or a.size == 0:
        return a.copy()
    padded = np.pad(a, (half, half), mode="edge")
    out = np.empty_like(a)
    for start in range(0, len(a), _CHUNK_FRAMES):
        stop = min(len(a), start + _CHUNK_FRAMES)
        out[start:stop] = fn(sliding_window_view(padded[start:stop + 2 * half], 2 * half + 1), axis=1)
    return out


def envelope(x: np.ndarray, cfg: DetectConfig) -> Envelope:
    band, low, hop_s = _band_power(x, cfg)
    frames = lambda s: max(0, round(s / hop_s))  # noqa: E731
    detick = _rolling(band, frames(cfg.tick_s) // 2, np.median)
    k = max(1, frames(cfg.smooth_s))
    smooth = np.convolve(detick, np.ones(k) / k, mode="same") if detick.size else detick
    band_db = 10 * np.log10(smooth + _EPS)
    baseline = _rolling(
        band_db, frames(cfg.baseline_s),
        lambda w, axis: np.percentile(w, cfg.baseline_percentile, axis=axis),
    )
    return Envelope(hop_s, band_db, baseline, 10 * np.log10(low + _EPS))


def _local_maxima(e: np.ndarray) -> np.ndarray:
    if len(e) < 3:
        return np.zeros(0, dtype=int)
    mid = e[1:-1]
    # >= on the left, > on the right: a flat-topped peak yields one index.
    return np.flatnonzero((mid >= e[:-2]) & (mid > e[2:])) + 1


def _suppress(idx: np.ndarray, strength: np.ndarray, min_gap: int) -> np.ndarray:
    """Greedy non-maximum suppression: strongest first, drop neighbours < min_gap."""
    taken = np.zeros(len(strength), dtype=bool)
    keep: list[int] = []
    for i in idx[np.argsort(-strength[idx], kind="stable")]:
        if taken[max(0, i - min_gap + 1):i + min_gap].any():
            continue
        taken[i] = True
        keep.append(int(i))
    return np.sort(np.array(keep, dtype=int))


def detect(x: np.ndarray, cfg: DetectConfig) -> Detection:
    env = envelope(x, cfg)
    t = env.times()
    active = t >= cfg.ignore_before_s
    if cfg.ignore_after_s is not None:
        active &= t <= cfg.ignore_after_s

    prom = env.band_db - env.baseline_db
    peaks = _local_maxima(prom)
    peaks = peaks[(prom[peaks] >= cfg.min_prominence_db) & active[peaks]]
    kept = _suppress(peaks, prom, max(1, round(cfg.min_gap_s / env.hop_s)))
    events = [Event(float(t[i]), float(env.band_db[i]), float(prom[i])) for i in kept]
    return Detection(env, cfg.min_prominence_db, events)
