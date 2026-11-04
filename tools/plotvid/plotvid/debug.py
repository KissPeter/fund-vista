"""Hand-check artefacts: envelope strip plot and an event-beep preview track."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from plotvid.audio import Detection

ROW_S = 20.0  # seconds per strip row
PX_PER_S = 100
ROW_H = 220
DB_SPAN = 50.0  # vertical range shown, top anchored near the loudest band level

BG = (255, 255, 255)
GRID = (225, 225, 225)
BAND = (30, 30, 30)
LF = (170, 170, 170)
THRESH = (210, 40, 40)
BASE = (240, 160, 160)
EVENT = (30, 90, 220)
DROPPED = (240, 140, 0)
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
        f"{title}  events={len(det.events)}  min prominence={det.min_prominence_db:.1f} dB  "
        f"(black: servo band, pink: baseline, red: baseline+min, grey: motor band "
        f"shifted to fit, blue: events, orange: dropped by width)",
        fill=TEXT, font=font,
    )
    top_db = float(np.percentile(env.band_db, 99.5)) + 3 if len(t) else 0.0
    # Motor band is far louder; shift it so its median sits mid-plot.
    lf_shift = (top_db - DB_SPAN / 2) - float(np.median(env.lf_db)) if len(t) else 0.0

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
        sel = (t >= t0) & (t < t0 + ROW_S)
        if sel.any():
            xs = x_of(t[sel])
            thr = env.baseline_db + det.min_prominence_db
            for series, colour in ((env.lf_db + lf_shift, LF), (env.baseline_db, BASE),
                                   (thr, THRESH), (env.band_db, BAND)):
                ys = y_of(series[sel], y0)
                draw.line(list(zip(xs.tolist(), ys.tolist())), fill=colour)
        for events, colour in ((det.events, EVENT), (det.dropped, DROPPED)):
            for ev in events:
                if t0 <= ev.time_s < t0 + ROW_S:
                    x = x_of(ev.time_s)
                    draw.line([(x, y0 + 4), (x, y0 + 14)], fill=colour, width=3)
                    draw.line([(x, y0 + 14), (x, y0 + ROW_H - 20)], fill=colour)
    img.save(out)


def _beep(sample_rate: int, freq: float, dur: float) -> np.ndarray:
    tt = np.arange(int(dur * sample_rate)) / sample_rate
    return 0.5 * np.sin(2 * np.pi * freq * tt) * np.hanning(len(tt))


def beep_track(x: np.ndarray, sample_rate: int, det: Detection, out: Path) -> None:
    """Stereo WAV: left = original audio; right = a high beep at each event and a
    low beep at each event the width gate dropped."""
    right = np.zeros_like(x)
    for events, beep in ((det.events, _beep(sample_rate, 1500, 0.03)),
                         (det.dropped, _beep(sample_rate, 400, 0.06))):
        for ev in events:
            i = int(ev.time_s * sample_rate)
            j = min(len(right), i + len(beep))
            if i < j:
                right[i:j] += beep[: j - i]
    stereo = np.stack([x, right], axis=1)
    pcm = (np.clip(stereo, -1, 1) * 32767).astype("<i2")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
