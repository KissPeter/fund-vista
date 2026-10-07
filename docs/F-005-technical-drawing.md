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
| `thin_lines` | Binarise at `threshold` first, smooth the **mask** (not the greyscale), so pale 1 px strokes survive. Re-binarise level follows `blur_radius` (`drawing.mask_level`). |
| `trace_upscale` 1–3 | Supersample before thinning for rounder curves; clamped by `PENPLOT_TRACE_MAX_PIXELS`. |

Preset (one click on `/penplot`, defaults of the CLI): `drawing.DRAWING_PRESET`.
Reproduce a local result on the page: upload the same image, press
**Apply technical-drawing preset**.

```bash
python -m backend.penplot.img2plot <url|file> -o out.svg   # same run_convert as the API
```

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
`--memory 512m`, 1000×707 drawing:

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

- **Docker (NAS / `Dockerfile`)**: installs `tesseract-ocr` (apt) and
  `pip install ./backend[ocr]`.
- **FastAPI Cloud**: builds from `pyproject.toml`/`uv.lock` and documents no
  way to install system packages, so there is **no Tesseract binary**: the
  option returns `ocr_unavailable` (request still succeeds) and the page
  disables the toggle with a note. Circles / thin lines / upscale work there.
  Text recognition on that host needs a backend that is a pure-Python wheel.

## Pluggable OCR backend

`drawing.OCR_BACKENDS[name] = (available(), reader_factory)`, selected with
`PENPLOT_OCR_BACKEND`. A reader is `ocr_text.SheetReader`:
`(sheet: ndarray, bounds: [(y0, y1)], whitelist: str) -> [text per row]`.
`SheetEngine` builds the sheets (one row per candidate crop, several
scale/shear/binarisation variants) and does the voting, so a replacement only
has to read rows; a per-crop classifier can slice `sheet[y0:y1]`. Requirements
for the alternatives: wheel-only install, ≲100 MB RAM at inference, digits and
`°` suffice (labels are 7–15 px italic CAD digits).

## Known limits

- Tuned on ~1000 px drawings (glyphs 6–15 px); larger scans are OCR'd at ≤1600 px.
- Digits/`°` only (`IKARUS 412`-style text is traced, not read).
- Partly hidden wheels (behind bogie frames) fail the full-ring test and stay traced.
- 2-character readings are dropped unless they end in `°` (`min_chars`).
