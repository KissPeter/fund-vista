"""Hand-check artefacts: envelope strip plot and a click-beep preview track."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from plotvid.audio import Detection

ROW_S = 20.0  # seconds per strip row
PX_PER_S = 100
ROW_H = 220
DB_SPAN = 60.0  # vertical range shown, anchored at the threshold

BG = (255, 255, 255)
GRID = (225, 225, 225)
HF = (30, 30, 30)
LF = (150, 150, 150)
THRESH = (210, 40, 40)
MEDIAN = (240, 160, 160)
CLICK = (30, 90, 220)
TEXT = (90, 90, 90)


def envelope_png(det: Detection, out: Path, title: str) -> None:
    env = det.envelope
    t = env.times()
    duration = float(t[-1] + env.hop_s) if len(t) else 0.0
    rows = max(1, int(np.ceil(duration / ROW_S)))
    width = int(ROW_S * PX_PER_S) + 60
    header = 28
    img = Image.new("RGB", (width, header + rows * ROW_H), BG)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    draw.text(
        (8, 8),
        f"{title}  clicks={len(det.clicks)}  median={det.median_db:.1f} dB  "
        f"MAD={det.mad_db:.2f}  threshold={det.threshold_db:.1f} dB  "
        f"(black: >cutoff, grey: motor band, blue: clicks)",
        fill=TEXT, font=font,
    )
    top_db = det.threshold_db + 0.35 * DB_SPAN

    def y_of(db: np.ndarray | float, y0: int):
        frac = (top_db - np.asarray(db)) / DB_SPAN
        return y0 + 4 + np.clip(frac, 0, 1) * (ROW_H - 24)

    for r in range(rows):
        y0 = header + r * ROW_H
        t0 = r * ROW_S
        x_of = lambda s: 50 + (s - t0) * PX_PER_S  # noqa: E731
        for s in range(int(t0), int(t0 + ROW_S) + 1):
            x = x_of(s)
            draw.line([(x, y0 + 4), (x, y0 + ROW_H - 20)], fill=GRID)
            draw.text((x - 6, y0 + ROW_H - 16), f"{s}s", fill=TEXT, font=font)
        for db, colour in ((det.median_db, MEDIAN), (det.threshold_db, THRESH)):
            y = float(y_of(db, y0))
            draw.line([(50, y), (x_of(t0 + ROW_S), y)], fill=colour)

        sel = (t >= t0) & (t < t0 + ROW_S)
        if sel.any():
            xs = x_of(t[sel])
            for series, colour in ((env.lf_db, LF), (env.hf_db, HF)):
                ys = y_of(series[sel], y0)
                draw.line(list(zip(xs.tolist(), ys.tolist())), fill=colour)
        for c in det.clicks:
            if t0 <= c.time_s < t0 + ROW_S:
                x = x_of(c.time_s)
                draw.line([(x, y0 + 4), (x, y0 + 14)], fill=CLICK, width=3)
                draw.line([(x, y0 + 14), (x, y0 + ROW_H - 20)], fill=CLICK)
    img.save(out)


def beep_track(x: np.ndarray, sample_rate: int, det: Detection, out: Path) -> None:
    """Stereo WAV: left = original audio, right = a short beep at each click."""
    beep_len = int(0.03 * sample_rate)
    tt = np.arange(beep_len) / sample_rate
    beep = 0.5 * np.sin(2 * np.pi * 1500 * tt) * np.hanning(beep_len)
    right = np.zeros_like(x)
    for c in det.clicks:
        i = int(c.time_s * sample_rate)
        j = min(len(right), i + beep_len)
        if i < j:
            right[i:j] += beep[: j - i]
    stereo = np.stack([x, right], axis=1)
    pcm = (np.clip(stereo, -1, 1) * 32767).astype("<i2")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
