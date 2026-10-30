# plotvid

Cuts pen-plotter recordings into short 9:16 clips at pen events. It is a standalone tool and imports nothing from `backend/`.

Status: PoC. Build steps 1–2 are done: config, probing, and pen-event detection with hand-check output. Segmentation and rendering come next (see the plan).

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
| `events.csv` | One row per detected pen event: time, level, prominence over the rolling baseline, and the gap to the previous event |
| `envelope.png` | A strip plot of the servo-band level (black), its baseline (pink), the detection line (red), the motor band (grey, shifted to fit) and detected events (blue) |
| `preview.mp4` | Only with `--preview`. The original video, with original audio on the left channel and a beep on the right at each detected event. Use it to listen for missed or false events. |
| `summary.json` | Probe data, the detector config and its statistics |

Sources are ordered by container `creation_time`, falling back to file mtime.

## Detection
A pen lift or drop is a short burst of noise, about 40–150 ms long, that is strongest above 8 kHz. The stepper motors are tonal and mostly lower: a direction change only shifts tones below 8 kHz, so a band starting at 2.5 kHz fired on it.

On a 5 ms hop the detector:
1. Takes STFT power in `band_lo_hz`..`band_hi_hz` (8–15 kHz).
2. Applies a short running median (`tick_s`) and a moving average (`smooth_s`) to shape each burst into one hump.
3. Builds a baseline from the `baseline_percentile` (20th) over ±`baseline_s`. A low percentile keeps dense trains of small details (a burst every ~0.17 s) above their own baseline, where a median would hide them.
4. Keeps peaks at least `min_prominence_db` above the baseline and `min_gap_s` apart.

Tuned on vid2 (pen without a plunger) against hand labels; the user judged the result "pretty accurate".

Known limitation: a sharp, loud tick in the band (about 10 ms) still fires. On vid2 that is the one significant false event, at 48.62 s. Real servo bursts are 35–100 ms wide at −6 dB, so a minimum-width gate would reject it. It is not added yet because about 10% of events are similarly narrow and haven't been checked.

## Licensing
NumPy (BSD-3), Pillow (HPND) and PyYAML (MIT) are permissive. ffmpeg's licence depends on how it was built; builds with libx264 are GPL, which is fine for rendering our own videos.
