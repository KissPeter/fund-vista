# plotvid

Cuts pen-plotter recordings into short 9:16 clips at pen events. It is a standalone tool and imports nothing from `backend/`.

Status: PoC, end to end. It covers config, probing, pen-event detection, the video ink check, segmentation, clip assignment, rendering and manifests.

## Requirements
- `uv` and Python 3.13
- `ffmpeg` and `ffprobe` on PATH

## Usage
```bash
cd tools/plotvid
uv sync
uv run python -m plotvid detect example.yaml --preview   # events + hand-check artefacts
uv run python -m plotvid plan example.yaml     # detect + ink check + segments -> manifests
uv run python -m plotvid render example.yaml   # manifests -> MP4 clips
uv run python -m plotvid all example.yaml      # plan, then render
uv run pytest -q
```

`plan` writes `out_dir/plan/`:

| File | Content |
|---|---|
| `clip_NN.json` | One manifest per clip: source path, each segment's source start and end, its strokes (duration, new ink, drawing/travel, long), ink score, DP weight, relaxation level, the zone, the seed and the final-photo frame |
| `strokes_<source>.csv` | Every stroke with its new-ink count and label, for hand checks |
| `summary.json` | Per-source label statistics, the clip counts tried and the zones that could not be filled |

Detection and the ink check are cached in `out_dir/cache/`, keyed by path, size, mtime and the `detect` and `ink` configs. Re-planning with other `strokes`, `segment` or `videos` settings is therefore instant.

`render` reads the manifests and writes `out_dir/clips/clip_NN.mp4`. A hand-edited manifest renders the same way.

`detect` writes `out_dir/detect/<source>/`:

| File | Content |
|---|---|
| `events.csv` | One row per detected pen event: time, level, prominence over the rolling baseline, and the gap to the previous event |
| `envelope.png` | A strip plot of the servo-band level (black), its baseline (pink), the detection line (red), the motor band (grey, shifted to fit) and detected events (blue) |
| `preview.mp4` | Only with `--preview`. The original video, with original audio on the left channel and a beep on the right at each detected event, and a low beep at each event the width gate dropped. Use it to listen for missed or false events. |
| `dropped.csv` | Same columns for events the width gate rejected (empty when the gate is off) |
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

Each event also records its burst width: how long the band level stays continuously within `width_drop_db` (6 dB) of its peak, measured at about 1 ms resolution. Servo bursts measure 13–100 ms, while the vid2 false ticks measure 4–8 ms (49.97 s and 48.62 s).

`min_width_s` (default 12 ms, `null` to switch it off) moves narrower events to `dropped.csv` instead of `events.csv`. The 12 ms default was accepted on vid2 by ear as an acceptable error rate. A better microphone is expected to improve results.

On vid2 at 12 ms the gate drops 70 of 456 events. That includes both known false ticks and none of the 30 events in the confirmed detail runs at 24–27 s and 41–44 s, whose narrowest is 13 ms. Whether the other dropped events are real still needs a listen.

## Ink check (drawing vs travel)
Pen events are untyped, so a stroke between two events may be a drawn line or a pen-up travel move. The video decides. Note that the vid2 phone is hand-held: the view pans, tilts and zooms. A fixed-pixel ink map therefore does not work.

1. Decode grayscale frames at 10 fps and 288 px wide (`ink.fps`, `ink.width`).
2. **Ink:** a black top-hat picks thin dark lines. The arm, pen body, rails and shadows are large dark blobs, which are masked (and dilated, so the pen tip is covered too). Paper is the bright, unmasked area, optionally limited by `ink.roi`.
3. **Registration:** consecutive frames are aligned with an affine ECC on the line image, seeded by phase correlation.
4. **New ink over a stroke:** pixels that are ink in at least 75% of the frames over the next 0.7 s (so a passing arm does not count), and that were paper but not ink at the stroke start. Old ink is dilated by 2 px against misregistration.
5. **Labels:** a stroke with at least `strokes.drawn_min_px` new pixels is drawing; anything less is travel. If almost no long stroke reads as drawing, the check is distrusted and every stroke becomes unknown, so any event may cut.

At 10 fps the dense 0.1–0.2 s detail strokes cannot be resolved, so their labels are noisy. The cut rules only rely on the labels of long strokes.

## Segments and clips
- **A segment** is 3–8 s of whole strokes. It ends `post_roll` after a long drawn stroke and starts `pre_roll` before 2–3 short strokes that lead into a long one. *Long* is `strokes.long_percentile` of stroke durations. Rolls are clamped halfway to the neighbouring event.
- **Relaxation**, when strict rules cannot fill a clip (the level is recorded per segment and per clip):

  | Level | Rules |
  |---|---|
  | 1 | Any lead-in before a long stroke |
  | 2 | Start at any event; end after any stroke that may be drawn |
  | 3 | Every event may cut |

- **Zones:** the plotting timeline (sources end to end) is split into N equal zones. Clip k only uses segments entirely inside zone k, so footage is never reused.
- **Per zone:** a weighted-interval DP picks the non-overlapping segments with the most ink. Shorter segment caps are tried before looser rules. A seeded weighted draw then takes a subset of 15–20 s (target 17 s) and plays it in chronological order.
- **`videos.count: auto`** picks the most clips, up to `max_count`, for which every zone can reach `min_total_s`. Clips are never shortened to fit.

## Rendering
- **Output:** 9:16 H.264 + AAC MP4 with `+faststart`, at the source frame rate (a phone's 30.013 average snaps to 30).
- **Size:** the largest 9:16 crop of the source at native resolution, with no upscaling. Too-tall sources lose top and bottom equally; too-wide ones lose left and right.
- **Audio:** each segment keeps the plotter's own sound, with 20 ms fades at every cut.
- **Ending:**
  - `final_photo`: silent, 2.5 s. The default is the frame in the source's last 5 s where the arm covers the least paper.
  - `outro`: 2.5 s. The default is a Pillow card with the plot name and `outro.text`. An image is silent; a video keeps its own audio.
- **Transition:** `cut` (concat) or `crossfade` (`xfade` + `acrossfade`, `crossfade_s`). Everything is one ffmpeg call.

## Licensing
NumPy (BSD-3), Pillow (HPND), PyYAML (MIT) and opencv-python-headless (Apache-2.0) are permissive. ffmpeg's licence depends on how it was built; builds with libx264 are GPL, which is fine for rendering our own videos.
