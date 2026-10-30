"""Servo-click detection from the audio track.

The pen servo makes a short (~15 ms) broadband click; the steppers are loud
up to ~4 kHz. So the detector looks at STFT energy in the click band
(``click_lo_hz``..``click_hi_hz``) on a fine hop, sets a per-file threshold from the robust
noise statistics of that envelope (median + k*MAD, never less than
``min_rise_db`` above the median), requires each peak to stand
``min_prominence_db`` above its local median (clicks are transients, motor
noise is sustained), and keeps peaks at least ``min_gap_s`` apart (stronger
peak wins).

The low-band envelope is computed alongside: it is what the planned
pause-length labeller (asymmetric pen-up delay) will measure.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from plotvid.config import DetectConfig

_EPS = 1e-12
_CHUNK_FRAMES = 16384  # bounds STFT memory on long recordings


@dataclass(frozen=True)
class Envelope:
    hop_s: float
    hf_db: np.ndarray  # energy above the cutoff, dB per frame
    lf_db: np.ndarray  # energy below the cutoff (motor band), dB per frame

    def times(self) -> np.ndarray:
        return (np.arange(len(self.hf_db)) + 0.5) * self.hop_s


@dataclass(frozen=True)
class Click:
    time_s: float
    level_db: float
    rise_db: float  # above the file-wide envelope median
    prominence_db: float  # above the local envelope median


@dataclass(frozen=True)
class Detection:
    envelope: Envelope
    median_db: float
    mad_db: float
    threshold_db: float
    clicks: list[Click]


def envelope(x: np.ndarray, cfg: DetectConfig) -> Envelope:
    sr = cfg.sample_rate
    hop = max(1, round(cfg.hop_s * sr))
    win = max(hop, round(cfg.window_s * sr))
    n_fft = 1 << (win - 1).bit_length()
    n_frames = max(0, 1 + (len(x) - win) // hop)
    if n_frames == 0:
        empty = np.zeros(0)
        return Envelope(hop / sr, empty, empty)

    window = np.hanning(win).astype(np.float32)
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    hf = (freqs >= cfg.click_lo_hz) & (freqs <= cfg.click_hi_hz)
    # Skip DC/sub-sonic rumble (handling, HVAC) in the motor band.
    lf = (freqs >= 50) & (freqs < cfg.click_lo_hz)
    norm = float((window**2).sum())

    hf_db = np.empty(n_frames)
    lf_db = np.empty(n_frames)
    for start in range(0, n_frames, _CHUNK_FRAMES):
        stop = min(n_frames, start + _CHUNK_FRAMES)
        idx = (np.arange(start, stop)[:, None] * hop) + np.arange(win)[None, :]
        spec = np.abs(np.fft.rfft(x[idx] * window, n=n_fft, axis=1)) ** 2
        hf_db[start:stop] = 10 * np.log10(spec[:, hf].sum(1) / norm + _EPS)
        lf_db[start:stop] = 10 * np.log10(spec[:, lf].sum(1) / norm + _EPS)
    return Envelope(hop / sr, hf_db, lf_db)


def _local_maxima(e: np.ndarray) -> np.ndarray:
    if len(e) < 3:
        return np.zeros(0, dtype=int)
    mid = e[1:-1]
    # >= on the left, > on the right: a flat-topped peak yields one index.
    return np.flatnonzero((mid >= e[:-2]) & (mid > e[2:])) + 1


def _suppress(idx: np.ndarray, strength: np.ndarray, min_gap: int) -> np.ndarray:
    """Greedy non-maximum suppression: strongest first, drop neighbours < min_gap."""
    keep: list[int] = []
    taken = np.zeros(0, dtype=int)
    for i in idx[np.argsort(-strength[idx], kind="stable")]:
        if taken.size and np.min(np.abs(taken - i)) < min_gap:
            continue
        keep.append(int(i))
        taken = np.append(taken, i)
    return np.sort(np.array(keep, dtype=int))


def detect(x: np.ndarray, cfg: DetectConfig) -> Detection:
    env = envelope(x, cfg)
    t = env.times()
    active = t >= cfg.ignore_before_s
    if cfg.ignore_after_s is not None:
        active &= t <= cfg.ignore_after_s

    e = env.hf_db
    stats = e[active] if active.any() else e
    median = float(np.median(stats)) if stats.size else 0.0
    mad = float(np.median(np.abs(stats - median))) if stats.size else 0.0
    threshold = median + max(cfg.k_mad * mad, cfg.min_rise_db)

    peaks = _local_maxima(e)
    peaks = peaks[(e[peaks] > threshold) & active[peaks]]
    r = max(1, round(cfg.prominence_window_s / env.hop_s))
    prom = np.array([e[i] - np.median(e[max(0, i - r):i + r + 1]) for i in peaks])
    keep = prom >= cfg.min_prominence_db
    peaks, prom = peaks[keep], prom[keep]
    min_gap = max(1, round(cfg.min_gap_s / env.hop_s))
    kept = _suppress(peaks, e, min_gap)
    prom_of = dict(zip(peaks.tolist(), prom.tolist()))

    clicks = [Click(float(t[i]), float(e[i]), float(e[i] - median), prom_of[int(i)])
              for i in kept]
    return Detection(env, median, mad, threshold, clicks)
