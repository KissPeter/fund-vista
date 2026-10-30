"""Click detection on synthetic recordings: noise + motor hum + servo clicks."""

from __future__ import annotations

import numpy as np
import pytest

from plotvid.audio import detect
from plotvid.config import DetectConfig

SR = 32000
CFG = DetectConfig(sample_rate=SR)


def synth(click_times, duration=12.0, gain=1.0, click_amp=0.25, seed=0):
    rng = np.random.default_rng(seed)
    n = int(duration * SR)
    t = np.arange(n) / SR
    x = 0.004 * rng.standard_normal(n)  # room noise
    # Stepper hum: loud low-frequency tones, switching on and off like moves.
    moving = (np.sin(2 * np.pi * 0.7 * t) > -0.3).astype(float)
    x += moving * (0.08 * np.sin(2 * np.pi * 180 * t) + 0.04 * np.sin(2 * np.pi * 540 * t))
    # Servo click: ~15 ms decaying broadband burst.
    k = int(0.015 * SR)
    burst = rng.standard_normal(k) * np.exp(-np.arange(k) / (0.004 * SR))
    for c in click_times:
        i = int(c * SR)
        x[i:i + k] += click_amp * burst[: n - i]
    return (gain * x).astype(np.float32)


def match(found, truth, tol=0.012):
    found = np.array(found)
    hits = sum(bool(found.size and np.min(np.abs(found - c)) <= tol) for c in truth)
    return hits / len(truth), hits / max(1, len(found))


def regular_clicks(rng_seed=1):
    rng = np.random.default_rng(rng_seed)
    gaps = rng.uniform(0.2, 0.5, size=30)
    times = np.cumsum(gaps) + 0.5
    assert times[-1] < 11.5  # must fit inside synth()'s 12 s
    return times


@pytest.mark.parametrize("gain", [0.05, 0.3, 1.0])
def test_recall_and_precision_independent_of_level(gain):
    truth = regular_clicks()
    det = detect(synth(truth, gain=gain), CFG)
    recall, precision = match([c.time_s for c in det.clicks], truth)
    assert recall >= 0.95
    assert precision >= 0.95


def test_threshold_tracks_recording_level():
    truth = regular_clicks()
    quiet = detect(synth(truth, gain=0.05), CFG)
    loud = detect(synth(truth, gain=1.0), CFG)
    # 20*log10(1/0.05) = 26 dB apart; the adaptive threshold follows.
    assert loud.threshold_db - quiet.threshold_db == pytest.approx(26.0, abs=1.5)


def test_min_gap_merges_double_hits():
    det = detect(synth([2.0, 2.05, 5.0]), CFG)
    times = [c.time_s for c in det.clicks]
    assert len(times) == 2
    assert times[0] == pytest.approx(2.0, abs=0.06)


def test_hum_alone_gives_no_clicks():
    det = detect(synth([]), CFG)
    assert det.clicks == []


def test_ignore_window():
    truth = [1.0, 3.0, 6.0, 9.0]
    cfg = DetectConfig(sample_rate=SR, ignore_before_s=2.0, ignore_after_s=7.0)
    det = detect(synth(truth), cfg)
    assert [round(c.time_s) for c in det.clicks] == [3, 6]


def test_empty_audio():
    det = detect(np.zeros(10, dtype=np.float32), CFG)
    assert det.clicks == []
