# plotvid

Cuts pen-plotter recordings into short 9:16 clips at pen events. It is a standalone tool and imports nothing from `backend/`.

Status: PoC. Build steps 1–2 are done: config, probing, and servo-click detection with hand-check output. Segmentation and rendering come next (see the plan).

## Requirements
- `uv` and Python 3.13
- `ffmpeg` and `ffprobe` on PATH

## Usage
```bash
cd tools/plotvid
uv sync
uv run python -m plotvid detect example.yaml --preview
uv run pytest -q
```

`detect` writes `out_dir/detect/<source>/`:

| File | Content |
|---|---|
| `events.csv` | One row per detected click: time, level, rise over the file median, prominence over the local median, and the gap to the previous click |
| `envelope.png` | A strip plot of the click-band envelope (black) and motor-band envelope (grey), with the threshold and median lines and detected clicks in blue |
| `preview.mp4` | Only with `--preview`. The original video, with original audio on the left channel and a beep on the right at each detected click. Use it to listen for missed or false clicks. |
| `summary.json` | Probe data, the detector config and its statistics |

Sources are ordered by container `creation_time`, falling back to file mtime.

## Detection
Clicks are found in the STFT energy between `click_lo_hz` and `click_hi_hz` (4–14 kHz by default, at a 5 ms hop). On `vid1` the servo click stands about 20 dB above plotting noise in that band, but only about 12 dB at 2–4 kHz, where the steppers are loud.

A peak counts as a click when it passes both gates:
- **Global gate:** above `median + max(k_mad·MAD, min_rise_db)` of the file.
- **Local gate:** at least `min_prominence_db` above the local median within `±prominence_window_s`.

Peaks closer together than `min_gap_s` are merged, and the stronger one wins.

## Licensing
NumPy (BSD-3), Pillow (HPND) and PyYAML (MIT) are permissive. ffmpeg's licence depends on how it was built; builds with libx264 are GPL, which is fine for rendering our own videos.
