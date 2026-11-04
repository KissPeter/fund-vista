"""ffmpeg subprocess helpers (decode only; rendering lives in render.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np


def extract_audio(path: Path, sample_rate: int) -> np.ndarray:
    """Decode the first audio stream to mono float32 in [-1, 1]."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
         "-map", "0:a:0", "-ac", "1", "-ar", str(sample_rate),
         "-f", "s16le", "-acodec", "pcm_s16le", "-"],
        check=True, capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def mux_preview(video: Path, wav: Path, out: Path) -> None:
    """Copy the video stream and replace audio with ``wav`` (for hand-checks)."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(video), "-i", str(wav),
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
         "-b:a", "160k", "-shortest", str(out)],
        check=True, capture_output=True,
    )
