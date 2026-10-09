# F-005 — Technical-drawing convert (OCR labels, exact circles, thin lines)

Status: in progress · Last updated: 2026-10-07 · Branch `feat/img-ocr-singlestroke`

CAD-style scans (dimension drawings, blueprints) trace badly with the generic
methods: printed numerals become scribbles, pale 1 px dimension/seat lines
vanish, wheels come out as polygons. Four **opt-in** `ConvertParams` fix that;
all default off, so the legacy path (and every cached result key) is untouched.

| Param | Effect |
|---|---|
| `ocr_text.enabled` (+ `min_conf`, `min_chars`) | Read labels, mask them out of the raster, redraw them in the ZnikoSL single-stroke face (`svgfont`) fitted to the source box/rotation. Weak or <`min_chars` readings stay traced lines (`ocr_low_confidence_words_kept_as_lines`). |
| `circles` | Hough circles → ring-support filter → concentric completion; the ring is blanked from the raster and drawn as an exact circle. |
| `strip_frame` | Detect a border drawn around the artwork (long lines near the edges, double borders included, >=3 of 4 sides) and crop to the inside of it, so the page's own frame/label/radius are the only frame. Warns `page_frame_removed` / `page_frame_not_found`. In the preset. OCR-word cache keys include the cropped size. |
| `thin_lines` | Binarise at `threshold` first, smooth the **mask** (not the greyscale), so pale 1 px strokes survive. Re-binarise level follows `blur_radius` (`drawing.mask_level`). |
| `trace_upscale` 1–3 | Supersample before thinning for rounder curves; clamped by `PENPLOT_TRACE_MAX_PIXELS`. |

Preset (one click on `/penplot`, defaults of the CLI): `drawing.DRAWING_PRESET`.
Reproduce a local result on the page: upload the same image, press
**Apply technical-drawing preset**.

```bash
python -m backend.penplot.img2plot <url|file|folder> -o out.svg   # same run_convert as the API
```

Batch/automation manual (folders, `--out-dir`, JSON report, exit codes, cron/CI
recipes): `docs/img2plot-cli.md`.

Ownership/licence metadata (`<title>`, `<desc>`, RDF `dc:`/`cc:`, generator) is
stamped into every result when configured: `docs/svg-metadata.md`.

## Where it lives

- `penplot/drawing.py` — `analyse` (OCR + circles), `build_ink` (mask), slots,
  OCR-word cache, `DRAWING_PRESET`, `OCR_BACKENDS`.
- `penplot/ocr_text.py` — `SheetEngine`: blob detection → contact-sheet voting →
  degree rule → repeat-label copy; `word_to_polylines` (font fitting).
- `penplot/arcs.py` — circle detection / exact polylines.
- `penplot/pipeline.py` — drawing-mode branch (tone → analyse → ink → methods);
  OCR text + circles join the static group (bypass linemerge-smooth-simplify
  like labels); `densify` before Chaikin so long edges don't balloon.

## Resource model (1–2 CPU, 512 MB)

Measured with the repo image under `fastapi run` (what FastAPI Cloud runs),
`--memory 512m`, 1000×707 drawing, **subprocess-Tesseract route** (the in-process
`tesserocr` route, now the default, is faster — see Deployment):

| Case | Latency | Peak mem |
|---|---|---|
| 1 request, 1 CPU, cold (OCR runs) | ~22 s | ~235 MiB |
| same image, slider tweak (OCR cached) | ~8–13 s | — |
| 3 simultaneous, 1 CPU, 1 worker | ≤36 s each, all 200 | ~305 MiB |
| 3–4 simultaneous, 2 workers | ≤36 s, all 200 | **~450–470 MiB** |

Guards (all env-tunable, see `.env.example`):

- **OCR budget** `PENPLOT_OCR_BUDGET_S=20`: past it the step stops, uses nothing
  partial, warns `ocr_time_budget_exceeded` (text stays traced). Request worst
  case ≈ queue (10) + OCR (20) + trace (~10–15) — under the ~60 s ceiling that
  `fastapi run --workers` enforces.
- **Machine-wide slots** (`flock` files in `<data>/ocr/`, so they hold across
  uvicorn workers; released by the kernel if a worker dies):
  OCR `ocr_max_concurrent=1`; heavy trace `trace_max_concurrent=1`. A request
  that cannot get the trace slot in `ocr_queue_s` runs at 1× with
  `trace_busy_low_quality`; no OCR slot → `ocr_busy`. Degrade, never OOM.
- **Memory**: supersampled trace capped at `trace_max_pixels=4 000 000`
  (~60 B/px peak; clamped with `trace_upscale_clamped`). OCR/circles run on an
  image capped at `ocr_max_dim_px=1600` and map back.
- **Cache**: OCR words per (image, contrast, brightness, remove_background,
  engine version) under `<data>/ocr/`; slider tweaks re-trace but never re-OCR.
- **Workers**: use **1 worker on 512 MB** (each worker adds ~100+ MB baseline;
  2 workers peaked at ~470 MiB). Recommended `PENPLOT_MAX_IMAGE_DIM_PX=2000`
  on that size (legacy default 3000 → ~6.7 MP working image).

## Deployment

OCR needs **no system package**: the `tesserocr` Linux wheel (x86_64 and
aarch64, ~5 MB) bundles libtesseract 5.5 and leptonica, and the English model
is vendored in `penplot/tessdata/` (tessdata_fast, Apache-2.0). Both are plain
Python-dependency installs, so it works wherever dependencies come from
`pyproject.toml`/`uv.lock`:

- **FastAPI Cloud** (`fastapi deploy` / `fastapi run`): verified in a container
  that imitates it (python:3.13-slim, deps from `uv.lock` only, no apt, no
  `tesseract` binary, `fastapi run`, 512 MB, 1 CPU) — OCR available, same
  strokes as the local run.
- **Docker image** (NAS): `pip install ./backend`, no apt step.
- **macOS dev**: no wheel; `pytesseract` + `brew install tesseract` is the
  fallback (`PENPLOT_OCR_BACKEND=auto` picks the wheel, else the binary).
- No backend usable -> `ocr_unavailable` warning, page toggle disabled.

Gotchas the container test found (both fixed, covered by tests):

- `tesserocr` imports `cysignals`, which installs signal handlers and so must
  be imported on the **main thread** — `ocr_text` imports it at app start (the
  convert itself runs on worker threads).
- `tesserocr` raises `RuntimeError` for a word with no text (pytesseract returned
  `""`); the reader skips those.
- `.dockerignore` now excludes `**/.venv/` (a host venv was being copied over
  the image's environment).

Measured under `fastapi run`, 512 MB, 1 CPU, no Tesseract binary:

| Case | Latency | Peak mem |
|---|---|---|
| Ikarus 1000x707, cold | ~5 s | ~190 MiB |
| locomotive 1024x374, cold | ~3 s | ~130 MiB |
| 3 simultaneous (Ikarus) | all 200, <=18 s | ~340 MiB |

(The subprocess `pytesseract` route measured ~22 s cold for Ikarus: in-process
`tesserocr` avoids ~1000 process launches.)

## Pluggable OCR backend

`drawing.OCR_BACKENDS[name] = (available(), reader_factory)`, selected with
`PENPLOT_OCR_BACKEND`. A reader is `ocr_text.SheetReader`:
`(sheet: ndarray, bounds: [(y0, y1)], whitelist: str) -> [text per row]`.
`SheetEngine` builds the sheets (one row per candidate crop, several
scale/shear/binarisation variants) and does the voting, so a replacement only
has to read rows; a per-crop classifier can slice `sheet[y0:y1]`. Requirements
for further alternatives: wheel-only install, ≲100 MB RAM at inference, digits and
`°` suffice (labels are 7–15 px italic CAD digits).

## Known limits

- Tuned on ~1000 px drawings (glyphs 6–15 px); larger scans are OCR'd at ≤1600 px.
- Digits/`°` only (`IKARUS 412`-style text is traced, not read).
- Partly hidden wheels (behind bogie frames) fail the full-ring test and stay traced.
- 2-character readings are dropped unless they end in `°` (`min_chars`).
