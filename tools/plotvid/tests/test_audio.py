"""Pen-event detection on synthetic recordings.

The scene mimics vid2: room noise, loud stepper tones below 8 kHz, servo
bursts (~60 ms of 8-15 kHz noise), motor tone changes that must not count,
and short loud ticks (a known limitation).
"""

from __future__ import annotations

import numpy as np
import pytest

from plotvid.audio import detect
from plotvid.config import DetectConfig

SR = 32000
CFG = DetectConfig(sample_rate=SR)


def _bandpass_noise(rng, n, lo, hi):
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / SR)
    spec[(f < lo) | (f > hi)] = 0
    out = np.fft.irfft(spec, n)
    return out / (np.abs(out).max() + 1e-12)


def synth(bursts=(), ticks=(), tone_changes=(), duration=12.0, gain=1.0,
          burst_amp=0.05, burst_s=0.06, seed=0):
    rng = np.random.default_rng(seed)
    n = int(duration * SR)
    t = np.arange(n) / SR
    x = 0.003 * rng.standard_normal(n)  # room noise
    # Stepper tones; the pitch steps at each tone change (direction change).
    pitch = np.full(n, 900.0)
    for i, c in enumerate(sorted(tone_changes)):
        pitch[int(c * SR):] = 900.0 + 400.0 * ((i + 1) % 2)
    phase = 2 * np.pi * np.cumsum(pitch) / SR
    x += 0.15 * np.sin(phase) + 0.06 * np.sin(3 * phase) + 0.03 * np.sin(5 * phase)
    k = int(burst_s * SR)
    env = np.hanning(k)
    for c in bursts:
        i = int(c * SR)
        x[i:i + k] += burst_amp * env[: n - i] * _bandpass_noise(rng, k, 8000, 15000)[: n - i]
    tk = int(0.004 * SR)
    for c in ticks:  # sharp, very short, much louder than a burst
        i = int(c * SR)
        x[i:i + tk] += 0.4 * rng.standard_normal(tk) * np.exp(-np.arange(tk) / (0.001 * SR))
    return (gain * x).astype(np.float32)


def match(found, truth, tol=0.05):
    found = np.asarray(found)
    hits = sum(bool(found.size and np.min(np.abs(found - c)) <= tol) for c in truth)
    return hits / max(1, len(truth)), hits / max(1, len(found))


def times(det):
    return [e.time_s for e in det.events]


def sparse_bursts():
    rng = np.random.default_rng(1)
    out = np.cumsum(rng.uniform(0.6, 1.5, size=10)) + 0.5
    assert out[-1] < 11.5
    return out


@pytest.mark.parametrize("gain", [0.05, 0.3, 1.0])
def test_sparse_bursts_independent_of_level(gain):
    truth = sparse_bursts()
    recall, precision = match(times(detect(synth(truth, gain=gain), CFG)), truth)
    assert recall >= 0.9
    assert precision >= 0.9


def test_dense_detail_train_is_resolved():
    # Small details: a servo burst every 0.17 s for 3 s.
    truth = np.arange(4.0, 7.0, 0.17)
    recall, precision = match(times(detect(synth(truth), CFG)), truth)
    assert recall >= 0.8
    assert precision >= 0.9


def test_loud_ticks_still_fire():
    # Known limitation: the STFT window smears a 4 ms tick over ~4 frames,
    # longer than the tick median removes. Pinned so a change is deliberate.
    det = detect(synth(ticks=[2.0, 4.0, 6.0, 8.0]), CFG)
    assert len(det.events) == 4


def test_motor_tone_changes_are_ignored():
    det = detect(synth(tone_changes=[3.0, 5.0, 7.0, 9.0]), CFG)
    assert det.events == []


def test_min_gap_merges_close_bursts():
    det = detect(synth([5.0, 5.05]), CFG)
    assert len(det.events) == 1


def test_ignore_window():
    cfg = DetectConfig(sample_rate=SR, ignore_before_s=2.0, ignore_after_s=7.0)
    det = detect(synth([1.0, 3.0, 6.0, 9.0]), cfg)
    assert [round(t) for t in times(det)] == [3, 6]


def test_empty_audio():
    assert detect(np.zeros(10, dtype=np.float32), CFG).events == []
