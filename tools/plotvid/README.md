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
| `clip_NN.json` | One manifest per clip: each segment's source, start and end, story part, strokes (duration, new ink, drawing/travel, long), ink score, DP weight and relaxation level; the parts' time ranges and per-part footage, the `story` invariant check, total and footage lengths, the seed and the final-photo frame |
| `strokes_<source>.csv` | Every stroke with its new-ink count and label, for hand checks |
| `summary.json` | Per-source label statistics, the clip counts tried (with the level each needed) and the footage used per part |

Detection and the ink check are cached in `out_dir/cache/`, keyed by path, size, mtime and the `detect` and `ink` configs. Re-planning with other `strokes`, `segment`, `videos` or `render` settings is therefore instant.

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

- **Long gaps:** a gap of more than `strokes.split_over_s` (20 s) between consecutive pen events, or between the start of the source and the first event, or between the last event and the end, is split into `ceil(gap / strokes.split_piece_s)` equal pieces (7 s by default), with synthetic events at the piece boundaries. This runs before the ink check, so each piece is measured on its own. A dense line with no pen-up move would otherwise make one stroke too long for any 3–8 s segment. Gaps at or under the threshold are untouched, and `null` turns the rule off. Synthetic events are flagged in the strokes CSV and manifests (`synthetic_start`).
- **Opening take:** no cut in the first 10 s (YouTube retention for spiro-curve-3.mp4 dropped 100% → 77% at its first cut, ~5 s). Each clip opens with one uninterrupted `first_min_s`–`first_max_s` (10–14 s) segment from the first part, which is that clip's only first-part segment. If the first file cannot supply one per clip, fewer clips are planned.
- **Final segment:** the last source's stretch of the timeline runs to the end of the recording, so the pen's trip home is not cut off. The plot's final segment ends at the source end and may be up to `final_extra_s` (3 s) longer than `max_s`. It always goes to the last clip.
- **Story parts:** the plotting timeline (files laid end to end, ordered by creation time) is split into `videos.parts` (3) equal time ranges, whatever the number of files. A segment belongs to a part only if it lies wholly inside it.
- **Every clip covers every part**, in chronological order, so it shows the drawing grow. The first segment always comes from the first part and the last from the last part (checked by `story_ok`, recorded as `story` in each manifest). Footage is never reused.
- **Length:** a clip is footage + final photo + outro minus the crossfade overlaps (`join_s` for each of the n+1 joins of n segments), within `min_total_s`..`max_total_s` (30–40 s, target 35 s). There are no segment-length caps. Per count and relaxation level the plan draws (seeded, `attempts`) a non-overlapping pool per part (a weighted-interval DP on jittered weights, so pools of varied segment lengths are tried), deals each block of consecutive segments to the clips in a random order (picks are spread through a part), then a local search moves, drops or swaps segments until every clip is in the window and the parts are balanced.
- **`videos.count: auto`** tries the count from `max_count` down and, per count, relaxation level 0 up to `max_relax`. The first count where every clip reaches `min_total_s` wins; `summary.json` lists the level for each count tried (`null` = none) and the pool and used footage per part. Clips are never shortened to get more of them; if nothing fits, no clips are planned.

## Rendering
- **Output:** 9:16 H.264 + AAC MP4 with `+faststart`, at the source frame rate (a phone's 30.013 average snaps to 30).
- **Size:** the largest 9:16 crop of the source at native resolution, with no upscaling. Too-tall sources lose top and bottom equally; too-wide ones lose left and right.
- **Rotation:** `render.rotate` is extra clockwise degrees on top of the file's own rotation tag. `auto` (default) turns an untagged landscape file 90 degrees counter-clockwise, because the Sony camera was turned to portrait but stores landscape frames with no tag. A 1920x1080 file then becomes 1080x1920 with no crop. Tagged and portrait files are left alone. Set `0` to disable, or `90`, `180`, `270` to force. The rotation applies before detection, ink check and rendering.
- **Audio:** each segment keeps the plotter's own sound, with 20 ms fades at the cut ends.
- **Ending:**
  - `final_photo`: silent, 2.5 s. The default is the frame in the source's last 5 s where the arm covers the least paper.
  - `outro`: 2.5 s. The default is a Pillow card with the plot name and `outro.text`. An image is silent; a video keeps its own audio.
- **Transition:** `crossfade` (default) joins segments, photo and outro with `xfade` (`crossfade_type`, 0.5 s `crossfade_s`) and `acrossfade`, so the audio fades out into the silent photo. `cut` uses hard concat. Each crossfade shortens the clip by its length; the planner counts that. Everything is one ffmpeg call.
- **Watermark:** `render.watermark` (PNG or SVG, relative to the YAML; `null` = none) is overlaid in the bottom-right corner of every clip except the outro, which already carries the logo. `watermark_width_frac` (0.2) sets its width and `watermark_margin_frac` (0.04) its gap to the edges, both as shares of the output width. The bundled `folder.yaml` uses `assets/watermark.svg`.

## Folder script (`plotvid.folder`)
Runs the same plan and render on one folder of videos and photos.

```bash
uv run python -m plotvid.folder /path/raw/lhbp [--config YAML] [--slide-s 3]
```

- **Config:** the bundled `folder.yaml` (outro `assets/outro.svg`) unless `--config` is given.
- **Output:** the folder path with `raw` replaced by `processed`: `/path/raw/lhbp` gives `/path/processed/lhbp/lhbp_01.mp4`, `_02`, ... (`lhbp.mp4` for one clip). Earlier clips of that name are deleted first, so a rerun replaces them. Without a `raw` component the output is `processed/<name>` next to the folder.
- **Temporary files:** `$TMPDIR/plotvid/<name>/` (generated config, analysis cache, stills, slideshow, `summary.json`). Delete the cache if you change the detection settings.
- **Videos** (`.mov`, `.mp4`, `.m4v`) go through detect, ink check and segment, ordered by container creation time. Story parts are equal time ranges of the whole timeline, not files (see above).
- **Photos** are picked up from the same folder automatically, ordered by EXIF capture time (mtime when missing). Each is cropped to the output frame and shown for `--slide-s` seconds. The slideshow closes every clip, before the outro.
- **Intro:** images in `intro/` inside the folder (if it exists) are shown first, 2 s each, with the same settings. The clip starts with them.
- **Outro:** `outro` from the YAML, relative to the YAML file. An SVG is converted with `rsvg-convert`. A video outro keeps its own audio.
- **Skipped files:** names starting with `.`, including macOS `._*` companions, and any other extension.
- **Final photo:** the YAML's `final_photo` is ignored, because the slideshow replaces it. A folder with photos but no videos gets the slideshow and outro only.
- **Failures:** the log ends with `-> <out>` on success, or `plotvid.folder: <reason>`.

## Licensing
NumPy (BSD-3), Pillow (HPND), PyYAML (MIT) and opencv-python-headless (Apache-2.0) are permissive. ffmpeg's licence depends on how it was built; builds with libx264 are GPL, which is fine for rendering our own videos.
