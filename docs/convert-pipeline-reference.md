# Convert pipeline — element reference

Date: 2026-09-23
Scope: the `POST /v1/convert` pipeline (`backend/penplot/pipeline.py`,
`backend/penplot/optimize.py`) plus the caching layers added in
docs/CR-002-convert-performance.md. This is a *decision aid*: what each
element is for, what it changes about the output, its knob, its cost, and
when you want it on or off. Follow the order below — it is the order the
pipeline runs in.

Two mental models matter more than any individual entry:

- **Geometry-affecting** stages change the SVG you get. Turning them off (or
  changing their tolerance) invalidates cached results and changes what the
  plotter draws.
- **Travel-only** stages leave the drawn geometry byte-identical; they only
  reorder strokes for pen-up distance. They are the only stages it is always
  safe to skip for previews.

Fast vector preview (P2) relies on exactly that split: it keeps all
geometry-affecting stages and skips the two travel-only stages.

---

## 0. Input handling

### 0.1 Raster path (PNG/photo) — preprocessing

`load_raster → maybe_downscale → remove_background → blur → contrast →
brightness → threshold_mask → strip_hatch`, then one or more *methods*.
Only ever runs for `is_vector=false` inputs; vector inputs skip all of it
(and its params never touch the vector result signature — see §4).

- `maybe_downscale` (cap `settings.max_image_dim_px`): hard CPU guard for
  huge uploads. Warns `image_downscaled_for_performance`.
- `blur`/`contrast`/`brightness`: prep the photo so the threshold gives a
  clean ink mask. Pure input-shaping; tune per source photo, no geometric
  meaning.
- `threshold_mask`: binary ink mask — the actual "what gets drawn" decision.
- `strip_hatch` (px): morphological opening that erases strokes thinner than
  `strip_hatch_px` (e.g. scanned hatch/crosshatch shading) while rebuilding
  thicker ones. Costly; only for scan cleanup.
- Methods (`METHOD_REGISTRY`; picks via `methods`, legacy `method`):
  - `contour` — edges of ink regions (outline drawings).
  - `centerline` — skeleton (thin lines for signs/logos).
  - `hatch` — pitch-stroked shading (`hatch_pitch_mm`, `hatch_angle_deg`).
  - `flow` — orientational hatching.
  Each is a generator over the *same* `(mask, gray)`; outputs concatenate in
  the order listed (e.g. shading first, outlines last).

### 0.2 Vector path — decode

`parse_svg_vectors` (expat + per-shape flatten): SVG → pixel polylines.
~8 s on a 1.3 MB map. **Disk-cached per image id** with a lazy 48 h TTL
(`svgparsecache.py`, atomic writes) → repeated first-converts skip it.

---

## 1. Transform chain (shared by both paths, post-decode)

### 1.1 `layout` — px → page mm

Scales everything into the pad/frame/margin-box of the requested page
(`page.size`, `page.margin_mm`, `page.padding_mm`). Runs *before* all
tolerances so they behave exactly like vpype's post-layout units.
Geometry/painting: yes (page placement). Knob: `page.*`.

### 1.2 Label + page frame (`static_lines`)

`render_label` + optional `page.frame` rect (skipped when the label border
already draws the same rect). These join the cleanup chain *after* layout and
are merged **separately** from image strokes at 1.2 — a joint linemerge would
fuse artwork into divider/frame across the gap strip at high tolerances.

### 1.3 `quantize` — snap to 0.02 mm grid

`read --quantization` equivalent. Snaps coordinates to a fixed grid, drops
consecutive duplicates and degenerate strokes. Makes every downstream
tolerance operate on the same canonical input — this is what keeps the whole
chain stable/deterministic. Geometry: yes (sub-0.02 mm jitter). Knob: fixed
`settings.quantization_mm`; never needs touching.

### 1.4 `linemerge` — join endpoint-touching strokes

The single most important stage for dense source maps. Greedy: seed from the
first unused line, absorb the first unused line whose endpoint touches the
growing stroke (four orientation checks, fixed priority), repeat. Reduces the
stroke count massively on city maps (a plot = many short SVG segments).
Effect: stroke count ↓ (≈50 %+ on dense maps), fewer pen lifts, cleaner
rendering. Geometry: yes (strokes coalesce). Cost: was the 5-minute hang for
a 76 k-point map; now single-pass over a tol-sized spatial hash
(`13d1978`) + test-gated byte-identity. Knob: `linemerge_tolerance_mm`
(max joint gap). Use: always on; raise barely (large gap = risky joins);
running it twice (image / static lines) by design.

### 1.5 `curvesmooth` — Chaikin smoothing (optional)

`curve_smooth` iterations of Chaikin (point-doubling, capped at 5). Makes
jagged vectors silky. Geometry: yes. Cost: each pass ~doubles points — the
only stage that *adds* work to downstream simplify. Off by default
(`curve_smooth = 0`); warn `curve_smoothed`. Use: only when the source
linework is visibly polygonal.

### 1.6 `linesimplify` — RDP, ring-preserving

Ramer–Douglas–Peucker that keeps closed/polygon runs intact (per-vertex
distance filter, no corner collapse). Points ↓ on long straight runs.
Geometry: yes (collinear points only; visual shape kept). Knob:
`linesimplify_tolerance_mm`. Cost: linear-ish; the counterweight to 1.5.
Use: always on; drives `stats.points` savings.

### 1.7 `linesort` — travel reorder (TRAVEL-ONLY)

Greedy nearest-neighbour + reversal so consecutive strokes start near where
the pen stopped. Drawn geometry byte-identical; only `pen_up_mm` /
`estimated_time_s` (and thus plot time) improve. Knob: `linesort` bool.
Cost: negligible after spatial-hash rewrite (Chebyshev ring + exact linear
fallback). **Skipped in fast vector preview** (P2). A quadratic reversion
here costs ~10 s per 5000 strokes — keep the perf gate.

### 1.8 `reloop` — seam rotation (TRAVEL-ONLY)

For each closed loop, rotate it so the seam sits nearest the previous pen
position (`np.argmin` seam search, first-index tie-break). Geometry
byte-identical; reduces pen-up jumps between loops. Knob:
`reloop_tolerance_mm` (max seam distance to force a loop). Cost: small.
**Skipped in fast vector preview** (P2). `reloop_tol < 0` = fast-path sentinel
(no-op).

### 1.9 `to_svg` — serialize

Assembles final page with `stroke_color` / `background` (data-URI only), loads
nothing external at runtime.

### 1.10 `stats` — the observable contract

`points`/`segments` before/after (counted around simplify+merge), `strokes`,
`pen_down_mm`, `pen_up_mm`, `estimated_time_s` (from `pen.draw_speed_mm_s`,
`pen.travel_speed_mm_s`, `pen_lift_s`). This is what the shop shows and what
tests assert — anything that changes stroke count or pen-up also moves stats
(and usually the result cache key).

---

## 2. `full_quality` / fast vector preview (P2 switch)

- `fast = is_vector and not full_quality` — default for vector inputs.
- Fast skips **1.7 + 1.8 only** (travel-only), warns `travel_optimization_off`,
  and the returned `vpype_command` recipe (see §5) honestly omits them.
- `full_quality = true` re-enables both for the final plot render.
- Raster inputs never take the fast path.
- The fast/`full_quality` distinction is itself part of the result cache key,
  so a normal preview and its full-quality counterpart are two distinct
  cached results.

---

## 3. Caches (what "later reuse" is available)

| Cache | What it saves | Key | TTL / invalidation |
|-------|---------------|-----|--------------------|
| Result sidecar (disk) | The *entire* convert (decode → to_svg) | `image_id` + effective param hash (content-addressed `{image_id}_{hash}_optimized.svg`) + `.json` stats/warnings/vpype sidecar | Same lazy TTL as results (48 h default); hash change = new file |
| Parsed-SVG (disk) | Vector decode (`parse_svg_vectors`) | `image_id` in `parsed_dir` | Lazy 48 h (`parsed_svg_ttl_hours`), atomic writes |
| Split geometry (Redis) | citymap/airports `render→import` round-trip (Overpass fetch + parse_geometry + merge/matching) | citymap: `(bbox, exact sorted layer set)` under `split-v1` — see §6 why per-layer keys are unsound; airports: mirrors its Overpass payload key | Redis TTL; values beyond the 32 MB budget skip gracefully |

---

## 4. Result identity (when do you get a cache hit?)

The signature that decides cache identity is **not** the full request — it is
the `effective_params_dump(params, is_vector)`:
- Strip **raster-only** fields on the vector path (`methods`, `threshold`,
  `blur_radius`, `contrast`, `brightness`, `remove_background`,
  `strip_hatch_px`, `hatch_pitch_mm`, `hatch_angle_deg`,
  `contour_simplify`, `centerline_prune_px`): changing any of them in the UI
  on an SVG does *not* invalidate the vector result.
- In fast mode, `linesort`/`reloop_tolerance_mm` are stripped identically
  (both values yield the same fast SVG).
- `full_quality` is **kept** — preview vs final are different results.

Rule of thumb: you get a cache hit whenever the drawing-facing parameters are
identical — page, merge/simplify/smooth tolerances, travel stages (respecting
fast mode), label, frame, pen, colors.

---

## 5. `vpype_command` honesty contract

The recipe mirrors *what actually ran* (P2): it keeps the 
`read --quantization → linemerge → linesimplify → linesort → reloop → layout →
write` skeleton, but omits stages that were skipped in fast mode
(`reloop_tol=None`/`linesort_on=false`) and never claims to reproduce the
bytes (see SPEC_V1.md divergence log — RDP variants, greedy vs 2-opt sort).

---

## 6. Caching soundness note (citymap layer split)

Per-layer split caching would be unsafe: `match_way_layer` can reassign a way
when a later layer gets added (e.g. `roads` vs `buildings`), so a per-layer
key could serve geometry that belongs to another layer. Citymap split keys
bind the **exact sorted layer set** + bbox. Airports keys bind their Overpass
payload (lat/lon/radius); width/minlen/zoom/labels re-render from the same
split.

---

## 7. Cost profile summary

| Stage | Cost class | Affects geometry | Off in fast preview |
|-------|-----------|------------------|-------------------|
| raster preprocess | linear, vector skips | no (creates it) | n/a (vector only) |
| `parse_svg_vectors` | ~8 s / MB map | no | no (cached on disk) |
| `layout` | O(points) | yes | no |
| `quantize` | O(points) | yes | no |
| `linemerge` | near-linear (was the O(n²) hang) | yes | no |
| `curvesmooth` | O(points × iters), point-doubling | yes | no (off by default) |
| `linesimplify` | linear | yes | no |
| `linesort` | near-linear | **no** | **yes** |
| `reloop` | small | **no** | **yes** |
| `to_svg` / `stats` | linear | – | no |

Where to look when a convert is slow: raster → preprocess/methods;
big vector → `parse_svg_vectors` (disk cache hot?) then `linemerge`/`linesort`
(regression gate in `backend/tests/test_penplot_optimize.py`).