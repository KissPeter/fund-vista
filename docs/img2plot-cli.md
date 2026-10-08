# img2plot CLI — batch image → plotter SVG

Turns raster drawings (CAD scans, dimension drawings, blueprints; PNG/JPG/WebP/
BMP/TIFF) into pen-plotter SVGs: line art traced as single centrelines, printed
numbers re-drawn in the ZnikoSL single-stroke font, wheels as exact circles, the
drawing's own border removed. It is the **same pipeline as `/penplot`** (it calls
`run_convert`), so any result can be reproduced on the page — see
[Reproducing a result on /penplot](#reproducing-a-result-on-penplot).

Typical cost for a ~1000 px drawing: **3–6 s on Linux/Docker** (in-process OCR),
**~10–20 s on macOS** (OCR runs the `tesseract` program once per sheet); ~270 MB RAM
per worker. On a Mac, Docker is the faster way to run big batches.

---

## 1. Setup (once)

Run everything from the `fund-vista/` directory.

**Local (macOS / Linux)**

```bash
cd fund-vista/backend
uv sync --group test          # creates backend/.venv with all dependencies
cd ..
# macOS only: the text step needs the tesseract program (Linux gets it from a wheel)
brew install tesseract
```

Check it works:

```bash
backend/.venv/bin/python -m backend.penplot.img2plot --help
```

Without a text-recognition backend the run still succeeds — numbers just stay as
traced lines and every image carries an `ocr_unavailable` warning.

**Docker (no Python setup, nothing else to install)**

```bash
cd fund-vista
docker build -t fundvista .
docker run --rm -v "$PWD/scans:/in:ro" -v "$PWD/plots:/out" fundvista \
    python -m backend.penplot.img2plot /in --out-dir /out --report /out/report.json
```

(Output files are owned by root. Docker Desktop/Colima only share paths under
your home directory.)

In the examples below `img2plot` stands for either
`backend/.venv/bin/python -m backend.penplot.img2plot` (local) or
`docker run … fundvista python -m backend.penplot.img2plot` (Docker).

---

## 2. Quick start

```bash
# one file                                  -> drawing.svg next to where you run it
img2plot drawing.jpg
# one file, chosen output
img2plot drawing.jpg -o plots/drawing.svg
# a whole folder (png/jpg/jpeg/webp/bmp/tif/tiff, not recursive)
img2plot scans/ --out-dir plots/
# explicit files and URLs, mixed
img2plot a.png https://example.org/b.jpg --out-dir plots/
# the full "production" form
img2plot scans/ --out-dir plots/ --report plots/report.json --skip-existing --jobs 2
```

Output naming: `<input name without extension>.svg` in `--out-dir`. Two inputs with
the same name (e.g. `a/x.png`, `b/x.png`) never overwrite each other — the second
becomes `x-<6 hex>.svg`. Files are written atomically (`.tmp` then rename), so a
killed run never leaves a half-written SVG.

**Several inputs, or a folder, require `--out-dir`.** `-o` is for a single input.

---

## 3. Exit codes and progress

| Code | Meaning |
|---|---|
| `0` | every image converted (or was skipped) |
| `1` | at least one image failed (the others are still written) |
| `2` | usage error: bad/missing option, no images found, invalid value — nothing was converted |

A bad file never stops the batch (unless `--fail-fast`): it is reported and the
run continues. Progress goes to **stderr**, one line per image, and a summary:

```
[1/3] FAIL  broken.png  PenPlotError: Uploaded file is not a readable image.
[2/3] ok    ikarus.svg  571 strokes  6.12s warnings=['page_frame_removed', ...]
[3/3] ok    loco.svg  518 strokes  3.02s warnings=['page_frame_not_found', 'curve_smoothed']
2 ok, 1 failed, 0 skipped of 3 in 9.2s
```

`-q/--quiet` drops the per-image lines (the summary stays). Nothing is written to
stdout, so it is safe to pipe.

### The JSON report (`--report FILE`)

```json
{
  "summary": {"total": 3, "ok": 2, "failed": 1, "skipped": 0, "seconds": 9.2},
  "results": [
    {"src": "/in/broken.png", "out": "/out/broken.svg", "status": "error",
     "error": "PenPlotError: Uploaded file is not a readable image.", "seconds": 0.0},
    {"src": "/in/ikarus.jpg", "out": "/out/ikarus.svg", "status": "ok",
     "strokes": 571, "seconds": 6.12,
     "warnings": ["page_frame_removed", "trace_upscale_clamped", "curve_smoothed"]}
  ]
}
```

`status` is `ok`, `error` or `skipped`. Useful filters (with `jq`):

```bash
jq '.summary' plots/report.json                                   # counts
jq -r '.results[] | select(.status=="error") | "\(.src)  \(.error)"' plots/report.json
jq -r '.results[] | select(.status=="ok" and (.warnings|length)>0)
       | "\(.out|split("/")[-1]): \(.warnings|join(","))"' plots/report.json
jq -r '.results[] | select(.status=="error") | .src' plots/report.json > retry.txt
```

---

## 4. Options

All options are optional. Unset ones use the **technical-drawing preset**
(`drawing.DRAWING_PRESET`), the same values as the `/penplot` preset button.

**Input / output / batch**

| Option | Meaning |
|---|---|
| `SRC …` | files, URLs (`http(s)://`) or folders; repeatable |
| `-o, --out FILE` | output for a single input |
| `--out-dir DIR` | output folder (created if missing); required for several inputs |
| `--report FILE` | write the JSON report |
| `--skip-existing` | skip an image if its output exists and is newer than the input (resumable / incremental runs); URLs are always converted |
| `--jobs N` | convert N images at once (default 1) — see [Performance](#6-performance-and-parallelism) |
| `--fail-fast` | stop at the first failure (exit 1) |
| `-q, --quiet` | no per-image lines |

**Frame, page and label** (the project's own framing)

| Option | Default | Meaning |
|---|---|---|
| `--keep-frame` | off | keep the border drawn in the source; by default it is detected and cropped away (`page_frame_removed`; none found → `page_frame_not_found`, nothing changes) |
| `--frame` | off | draw the project's page frame (rounded rectangle at the margin) |
| `--frame-radius-mm R` | 2 | corner radius of that frame |
| `--label "TEXT"` | none | add the title-block strip with this text (ZnikoSL font) |
| `--label-font NAME` | `znikoslsvginot` | also `futural`, `futuram`, `simplex`, … |
| `--size` / `--orientation` | `A4` / `landscape` | `A3`/`A4`/`A5`/`Letter`; `portrait`/`landscape` |
| `--margin-mm` / `--padding-mm` | 10 / 0 | outer margin / padding inside it |

**Tracing**

| Option | Default | Meaning |
|---|---|---|
| `--threshold N` | 215 | 0–255; pixels darker than this are ink. Raise it for pale, grey lines; lower it for noisy scans |
| `--contrast X` / `--brightness N` | 1.0 / 0 | applied before OCR and tracing (0–3 / −100..100) |
| `--blur X` | 0.7 | smoothing of the ink mask (0–10) |
| `--upscale N` | 3 | 1–3: supersample before thinning for rounder curves; the server caps it at 4 MP, so big images run at 2×/1× (`trace_upscale_clamped`) |
| `--simplify X` | 0.7 | contour simplification (px) |
| `--prune-px N` | 3 | drop skeleton spurs shorter than N px |
| `--curve-smooth N` | 3 | 0–3 corner-rounding passes |
| `--linemerge-mm X` / `--linesimplify-mm X` | 0.5 / 0.1 | pen-path clean-up tolerances |

**Text and circles**

| Option | Default | Meaning |
|---|---|---|
| `--no-ocr` | off | do not read labels; they stay as traced lines |
| `--min-conf N` | 20 | reading agreement (%) needed to replace a label (5–100) |
| `--min-chars N` | 3 | shortest reading accepted (`7°` always passes) |
| `--no-circles` | off | skip exact-circle detection |

---

## 5. Recipes

```bash
# Source border removed (default), project frame with rounded corners + title block
img2plot scans/ --out-dir plots/ --frame --frame-radius-mm 6 --label "BUS PLANS"

# Keep the drawing's own frame, no project frame
img2plot scans/ --out-dir plots/ --keep-frame

# Pale grey scans: lines missing -> raise the threshold; noisy scans -> lower it
img2plot scans/ --out-dir plots/ --threshold 235
img2plot scans/ --out-dir plots/ --threshold 180

# Fast draft pass (no text, no circles, 1x trace)
img2plot scans/ --out-dir draft/ --no-ocr --no-circles --upscale 1

# Portrait A3 sheets
img2plot scans/ --out-dir plots/ --size A3 --orientation portrait --margin-mm 15

# Resume after a crash / only convert new or changed inputs
img2plot scans/ --out-dir plots/ --skip-existing --report plots/report.json
```

### Automation

**Nightly cron (macOS/Linux)**

```cron
0 2 * * *  cd /opt/fund-vista && backend/.venv/bin/python -m backend.penplot.img2plot \
    /data/incoming --out-dir /data/plots --skip-existing --report /data/plots/report.json -q \
    || echo "img2plot failed" | mail -s "plots" you@example.org
```

**Shell: stop the pipeline on any failure, keep going otherwise**

```bash
set -u
img2plot scans/ --out-dir plots/ --report plots/report.json --skip-existing
rc=$?
if [ "$rc" -eq 2 ]; then echo "bad invocation" >&2; exit 2; fi
if [ "$rc" -eq 1 ]; then
  jq -r '.results[] | select(.status=="error") | "\(.src)\t\(.error)"' plots/report.json >&2
  exit 1
fi
```

**Makefile (incremental by file timestamp; or use `--skip-existing`)**

```make
IMG2PLOT ?= backend/.venv/bin/python -m backend.penplot.img2plot
plots/%.svg: scans/%.png
	$(IMG2PLOT) $< -o $@ -q
all: $(patsubst scans/%.png,plots/%.svg,$(wildcard scans/*.png))
```

**GitHub Actions**

```yaml
- uses: actions/checkout@v4
- uses: astral-sh/setup-uv@v5
- run: cd fund-vista/backend && uv sync
- run: |
    cd fund-vista
    backend/.venv/bin/python -m backend.penplot.img2plot ../scans --out-dir ../plots \
      --report ../plots/report.json -q
- uses: actions/upload-artifact@v4
  if: always()
  with: { name: plots, path: plots/ }
```

(The Linux wheel provides OCR; no `apt` step needed.)

---

## 6. Performance and parallelism

Measured (Linux/Docker): 1000×707 drawing ≈ 3–6 s cold (OCR + trace), ≈ 2–3 s when
the OCR words are cached; 1024×374 ≈ 3 s; one image uses ~190–280 MB. macOS runs
about 2–3× slower (subprocess Tesseract).

- **Sequential (`--jobs 1`, default) is the safest** and already fast. Dozens of
  images take minutes.
- **`--jobs N`** runs N worker processes. OCR and the memory-heavy trace are
  guarded by *machine-wide* slots (one at a time each by default), so extra jobs
  mostly overlap the lighter stages rather than multiplying speed. On a bigger
  machine raise them:

  ```bash
  PENPLOT_OCR_MAX_CONCURRENT=4 PENPLOT_TRACE_MAX_CONCURRENT=4 \
      img2plot scans/ --out-dir plots/ --jobs 4
  ```

  Budget ~300 MB RAM per concurrent trace.
- **OCR cache:** read words are cached per (file content, contrast, brightness,
  image size) under `$PENPLOT_DATA_DIR/ocr` (default `backend/.data/ocr`). Re-running
  with different tracing options (threshold, smoothing, frame, page) does **not**
  re-read the labels. Point `PENPLOT_DATA_DIR` at a fixed folder in automation so
  the cache survives between runs.
- **Batch defaults differ from the web server on purpose:** there is no HTTP
  timeout, so the CLI waits for a free slot (`PENPLOT_OCR_QUEUE_S=3600`) and gives
  OCR 120 s per image (`PENPLOT_OCR_BUDGET_S=120`) instead of degrading. Set those
  variables yourself to override.

Environment variables (all optional): `PENPLOT_DATA_DIR`, `PENPLOT_OCR_BACKEND`
(`auto`, `tesserocr`, `tesseract`, `none`), `PENPLOT_OCR_BUDGET_S`,
`PENPLOT_OCR_QUEUE_S`, `PENPLOT_OCR_WORKERS`, `PENPLOT_OCR_MAX_CONCURRENT`,
`PENPLOT_TRACE_MAX_CONCURRENT`, `PENPLOT_TRACE_MAX_PIXELS`,
`PENPLOT_MAX_IMAGE_DIM_PX` (see `backend/.env.example`).

---

## 7. Warnings reference (`warnings` in the report)

| Warning | Meaning / what to do |
|---|---|
| `page_frame_removed` | a border was found and cropped away |
| `page_frame_not_found` | no frame detected; image used as is (needs ≥ 3 of 4 long edge lines) |
| `ocr_low_confidence_words_kept_as_lines` | some readings were too weak/short and stayed traced — normal; lower `--min-conf` to accept more |
| `ocr_unavailable` | no text backend on this machine (macOS: `brew install tesseract`) |
| `ocr_time_budget_exceeded` | OCR hit its time budget; text stayed traced — raise `PENPLOT_OCR_BUDGET_S` |
| `ocr_busy` / `trace_busy_low_quality` | could not get a slot in time (only with a short `PENPLOT_OCR_QUEUE_S`); that image ran without OCR / at 1× |
| `trace_upscale_clamped` | supersampling reduced to stay within the pixel cap (large image) |
| `image_downscaled_for_performance` | longer side exceeded `PENPLOT_MAX_IMAGE_DIM_PX` (3000) |
| `curve_smoothed` | informational: corner rounding applied |

---

## Reproducing a result on /penplot

1. Open `/penplot`, upload the same image.
2. Press **Apply technical-drawing preset** (this is exactly the CLI default).
3. Mirror any flags you used:

| CLI flag | `/penplot` control |
|---|---|
| `--threshold`, `--contrast`, `--brightness`, `--blur` | Method: threshold, contrast, brightness, blur_radius |
| `--simplify`, `--prune-px`, `--curve-smooth` | Method: contour_simplify, centerline_prune_px, curve_smooth |
| `--upscale` | 1b: trace_upscale |
| `--no-ocr`, `--min-conf`, `--min-chars` | 1b: ocr_text, ocr_min_conf, ocr_min_chars |
| `--no-circles` | 1b: circles |
| `--keep-frame` | 1b: strip_frame (unticked) |
| `--frame`, `--frame-radius-mm`, `--size`, `--orientation`, `--margin-mm`, `--padding-mm` | 3. Page & pen |
| `--label`, `--label-font` | 4. Label |
| `--linemerge-mm`, `--linesimplify-mm` | 2. Cleanup |

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `error: several inputs need --out-dir` (exit 2) | add `--out-dir DIR` |
| `error: no images found` | folder has no png/jpg/jpeg/webp/bmp/tif/tiff (not recursive — pass subfolders) |
| `error: invalid option value: …` | the value is out of range (e.g. `--upscale 9`) |
| every image has `ocr_unavailable` | macOS: `brew install tesseract`; Linux: reinstall dependencies (`uv sync`) |
| lines or numbers missing in the SVG | pale drawing → raise `--threshold`; try `--contrast 1.3` |
| bits of the source frame remain | frame is broken/partial (needs ≥ 3 long sides) — crop the image first |
| a number was misread | raise `--min-conf` so weak readings stay as lines; check `ocr_low_confidence…` |
| `Image processing failed` for one file | corrupt/unsupported file; see `error` in the report, the rest of the batch is unaffected |
| runs slower than listed | first run reads labels; later runs hit the OCR cache — keep `PENPLOT_DATA_DIR` stable |
