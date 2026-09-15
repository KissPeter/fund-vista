# Pen-plot image page (`GET /penplot`)

Date: 2026-09-15
Sources: `backend/penplot/ui.py`, `backend/penplot/templates/penplot.html`,
`backend/penplot/templates/partials/*`, `backend/penplot/router.py`,
`backend/penplot/schemas.py`, `backend/penplot/pipeline.py`,
`backend/penplot/methods.py`, `backend/penplot/optimize.py`,
`backend/main.py`

The image page is the raster/vector → plotter-SVG converter demo. It owns
the shared Jinja partials and the shared convert pipeline; `/citymap` and
`/airports` reuse both.

## Route wiring

- `GET /penplot` (`backend/penplot/ui.py::penplot_ui`) renders
  `penplot.html` with code-accurate defaults:
  `ConvertParams().model_dump()`, `methods = [contour, centerline, hatch,
  flow]`, `page_sizes = sorted(PAGE_SIZES_MM)` (A3/A4/A5/LETTER),
  `orientations = [portrait, landscape]`, `fonts = LABEL_FONTS`,
  `line_colors = LINE_COLORS`, `backgrounds = BACKGROUNDS`.
- Registered in `backend/main.py` **before** the catch-all fund proxy so
  `/v1/*` and `/penplot` win over `/{prefix}/{path}`.
- Page itself is unthrottled static markup; every expensive call it makes
  (`POST /v1/images`, `POST /v1/convert`, `GET /v1/results/*`) is behind
  the shared per-IP rate limiter (`backend/penplot/ratelimit.py`).

## UI controls (what the user sees)

Page layout: left column = fieldsets 1–5 + reset, right column =
`partials/_result_panel.html` (status, preview `<img>`, stats table,
warnings, vpype recipe, download link).

### 1. Image (page-local, `penplot.html`)

| Control | Element | Function |
|---|---|---|
| File picker | `<input type=file id=file accept="image/*,.svg">` | Select raster (png/jpg/webp/bmp/tif) or SVG. On `change`: `upload()` then `convert()`. Retains `currentFile` for the §4.3 silent re-upload retry. |
| `#meta` | `<p class=meta>` | Shows `image_id` prefix, format, WxH, `(vector)` flag, warnings. |

### 2. Method (`partials/_method.html`, legend "1. Method" here)

| Control | Backend field | Function |
|---|---|---|
| `m_contour`, `m_centerline`, `m_hatch`, `m_flow` checkboxes | `ConvertParams.methods` (multi-select, ordered) | Which generators run; output concatenated before optimize chain. Defaults from server. |
| `threshold` 0–255 | `threshold` | Binarization cutoff. |
| `blur_radius` 0–10 | `blur_radius` | Pre-blur before threshold. |
| `contrast` 0–3, `brightness` −100–100 | `contrast`, `brightness` | Linear stretch + offset before threshold. |
| `remove_background` checkbox | `remove_background` | Flatten uneven paper background (raster only). |
| `strip_hatch_px` 0–20 | `strip_hatch_px` | Morphological opening on ink mask: erases thin hatch texture, keeps bold strokes. 0 = off. |
| `hatch_pitch_mm` 0.2–5, `hatch_angle_deg` 0–175 | `hatch_pitch_mm`, `hatch_angle_deg` | Hatch spacing/direction (+90° cross pass in dark areas). Ignored by contour/centerline/flow. |
| `contour_simplify` 0–20 | `contour_simplify` | approxPolyDP epsilon (px) for contour/centerline branches. |
| `centerline_prune_px` 0–50 | `centerline_prune_px` | Drop skeleton spur branches shorter than N px. Centerline only. |
| `curve_smooth` 0–3 | `curve_smooth` | Chaikin corner-cutting passes in mm space. |

### 3. Cleanup — vpype chain (`partials/_cleanup.html`)

| Control | Backend field | Function |
|---|---|---|
| `linemerge_tolerance_mm` 0–5 | `linemerge_tolerance_mm` | Join near-touching endpoints (`optimize.linemerge`). |
| `linesimplify_tolerance_mm` 0–2 | `linesimplify_tolerance_mm` | RDP simplify (`optimize.linesimplify`). |
| `linesort` checkbox | `linesort` | Reorder strokes to cut pen-up travel (`optimize.linesort`). |
| `reloop_tolerance_mm` 0–2 | `reloop_tolerance_mm` | Close near-closed loops (`optimize.reloop`). |

### 4. Page & pen (`partials/_page_pen.html`)

| Control | Backend field | Function |
|---|---|---|
| `page_size` select | `page.size` (A5/A4/A3/LETTER) | Physical page; dims from `PAGE_SIZES_MM` in `config.py`. |
| `page_orientation` select | `page.orientation` | portrait/landscape swap. |
| `page_margin_mm` 0–50, `page_padding_mm` 0–20 | `page.margin_mm`, `page.padding_mm` | Outer margin + inner artwork padding. |
| `page_frame` checkbox, `page_frame_radius_mm` 0–20 | `page.frame`, `page.frame_radius_mm` | Whole-page rounded border. |
| `draw_speed_mm_s` 5–200, `travel_speed_mm_s` 10–500, `pen_lift_s` 0–2 | `pen.*` | Stats-only: estimated plot time (`pen_down/up_mm`, `estimated_time_s`). No geometry effect. |

### 5. Label — title block (`partials/_label.html`)

| Control | Backend field | Function |
|---|---|---|
| `label_enabled` | `label.enabled` | Reserve bottom strip and draw title block. |
| `label_text` (120 chars) | `label.text` | Label string. City/airport imports auto-stamp it. |
| `label_align` (left/right/fill) | `label.align` | Text justification in strip. |
| `label_height_mm` 2–12 | `label.height_mm` | Glyph height; also sizes the reserved strip (`labels.label_reserve_mm`). |
| `label_font` select | `label.font` | `futural/futuram/simplex` (single-stroke Hershey) + `excalifont/comic-shanns/nunito` (TTF outlines, pen draws each stem twice). |
| `label_border`, `label_pad_left/right_mm`, `label_border_radius_mm` | `label.border`, `pad_*`, `border_radius_mm` | Border frame + insets + corner radius. |

### 6. Display (`partials/_display.html`, preview-only)

| Control | Backend field | Function |
|---|---|---|
| `line_color` (black/white/red/blue) | `line_color` | SVG stroke color only. Never affects geometry/stats (`backgrounds.LINE_COLORS` allowlist). |
| `background` (none/dark-texture/light-texture) | `background` | Vendored JPEG embedded as `<image>` fill (`backgrounds.py`). Never affects geometry/stats. Suggested pairs noted in help text. |

### Result panel (`partials/_result_panel.html`)

`#status` (aria-live message), `#preview` (`<img src=svg_url>`),
`#stats` table (strokes, points before→after, segments before→after,
pen down/up mm, est. time s), `#warnings` list, `#vpype` recipe code,
`#download` link (`download="plot_optimized.svg"`).

### Form chrome

`#resetBtn` → `form.reset()` to server-rendered defaults + `syncOutputs()`
+ debounced convert. All range inputs have paired `<output id=..._val>`
synced by `syncOutputs()`.

## JS functions (`partials/_convert.html` + page script)

Shared block included by all three pages:

- `readParams()` — scrapes every control id above into a `ConvertParams`
  JSON body (methods from checked `m_*`, numbers via `parseFloat/parseInt`).
- `upload(file)` — `POST /v1/images` (multipart `file`), sets global
  `imageId`, writes `#meta` line.
- `convert(retried)` — `POST /v1/convert {image_id, params}`; on success
  calls `render(body)`. Handles `404 image_not_found` with §4.3 silent
  re-upload via retained `currentFile` (retry once), `429` → "Rate
  limited" message, else `Convert failed (code): message`.
- `render(body)` — sets `#preview.src` + `#download.href = svg_url`,
  fills stats table, `vpype_command`, warnings list.
- `scheduleConvert()` — 500 ms debounce; if `busy`, sets `pending=true`
  instead of overlapping (coalesced in `drain()`).
- `setBusy(on)/drain()` — disables `#params input/select/button + #file`
  while in flight, toggles `form.busy` opacity; `drain()` re-arms the
  debounce timer if a slider moved mid-flight.
- Page-local: file-input `change` listener only. Global `#params input`
  listener fires `scheduleConvert` + `syncOutputs` on any slider.

## Backend (what the server does)

- `POST /v1/images` (`router.upload_image`): size-guard (25 MB),
  `imaging.sniff_extension`, content-addressed store
  (`ImageStore.put_image_bytes`, sha256 `image_id`), probe
  (`imaging.parse_svg_vectors` for SVG → `is_vector=true`,
  `imaging.probe_raster` otherwise), `low_resolution_for_a4` warning
  under 100 DPI. Rate-limited.
- `POST /v1/convert` (`router.convert`): find image by id (404
  `image_not_found`), re-probe dims, `asyncio.to_thread(run_convert, …)`
  so the GIL-bound OpenCV/hatch march never stalls the event loop, store
  result, return `{image_id, svg_url, vpype_command, stats, warnings}`.
- `run_convert` (`pipeline.py`): label strip reserve → raster branch
  (load → optional downscale to 3000 px → remove_background → gray
  preprocess → ink mask) or vector branch (parse SVG paths directly,
  shading sliders ignored) → run each selected method in order
  (`methods.py`: `ContourMethod` via OpenCV findContours, `CenterlineMethod`
  via Zhang-Suen thinning + skeleton walk, `HatchMethod` tonal hatch +
  dark cross-hatch, `FlowMethod` deterministic streamlines) → `layout()`
  to page mm (quantize 0.02 mm) → label + frame lines → vpype chain
  (`linemerge → curvesmooth → linesimplify → linesort → reloop`) →
  `to_svg()` with line_color/background layers → stats (points/segments
  before→after, pen down/up, est. time) → `build_vpype_command()` recipe.
- `GET /v1/images/{id}`, `GET /v1/results/{filename}`: metadata / serve
  `{sha}_{hash}_optimized.svg` (path-traversal guarded).
- Errors: uniform `{"error": {"code","message"}}` envelope
  (`PenPlotError` + `RequestValidationError → invalid_params`);
  `extra="forbid"` on all Pydantic schemas so typo'd sliders 422 loudly.
