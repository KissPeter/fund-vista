# Convert performance — profiling findings

Date: 2026-09-23
Repro: live NAS backend (`fundvista:nas`, `docker exec` into the running container)
Linked plan: GH issues in this repo (see §6).

## TL;DR

`POST /v1/convert` on the designer's vector path is CPU-bound inside
`backend/penplot/optimize.py`, and it is **not** the whole machine's CPU being
saturated — a single convert pegs **exactly one CPU core** to ~100% (GIL-bound
pure Python), leaving the container's `--cpus=3` budget mostly idle.

The dominant cost on realistic city-map inputs is `linemerge`:

| Input | source points | `linemerge` | whole convert |
|---|---|---|---|
| medium saved map (141 KB SVG) | 8 281 | **6.9 s (≈74%)** | 9.5 s |
| large saved map (1.3 MB SVG) | 75 594 | **> 5 min (never returned)** | ≥ 5 min |

`linemerge` is a greedy endpoint-join implemented as nested Python loops that
re-scans the whole workspace for every extension and repeats until no joins
remain — worst case O(passes · n²) with a `math.hypot` / `dist` pair per
candidate (`optimize.py:66`). That is the "convert hangs for 5 minutes" the
shop reports, and it matches 1:1: 76 k-point maps never leave this stage.

Secondary costs, already measurable on the medium map:
- `linesort` (greedy nearest-neighbour, O(n²)): ~1.4 s.
- `reloop`: ~1.4 s (per-loop `min(range(...), key=...)` Python overhead).
- `parse_svg_vectors` on the 1.3 MB map: ~8 s (expat + per-shape walk).

## 1. How a designer convert runs (vector branch)

Citymap/Airport tabs upload a real-geometry SVG, then convert it. The input is
a vector (`is_vector=true`), so the raster methods (contour/centerline/hatch/
flow) are **skipped entirely**. The hot path is:

```
parse_svg_vectors                        # expat XML + per-shape flatenning
  → layout (px → A4 mm)                  # O(points), python
  → quantize (0.02 mm snap)              # O(points), python
  → linemerge (tol 0.5 mm)               # ★ greedy O(passes·n²), pure python
  → curvesmooth (Chaikin, 1 iter)        # ~doubles points
  → linesimplify (RDP)                   # recursion, ok
  → linesort (nearest-neighbour)         # ★ greedy O(n²), pure python
  → reloop (seam rotation)               # per-closed-ring min() scans
  → to_svg + stats (polyline_length…)    # O(points), python
```

All of it runs under the GIL via `asyncio.to_thread(run_convert, …)`
(`router.py:300`); the pure-Python geometry means one convert holds one core.

## 2. Profiling method (reproducer)

Profiled live inside the container so the measured environment == production
(Python 3.13.15, numpy 2.5.3, cv2 5.0.0, 4 uvicorn workers, `--cpus=3.0`).

### cProfile harness

`run_convert` is a pure synchronous function — no HTTP needed. The harness
below mirrors exactly what the designer tabs send (lineart bundle, A4
portrait, auto-stamped label) and wraps `run_convert` in `cProfile`, plus
colours per-stage timings already emitted by `pipeline.py` at DEBUG.

```python
# /tmp/profile_convert.py — run:  python3 -u /tmp/profile_convert.py <svg> [label]
import cProfile, io, logging, pstats, sys, time
from pathlib import Path
logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)

from backend.penplot import imaging
from backend.penplot.config import Settings
from backend.penplot.pipeline import run_convert
from backend.penplot.schemas import ConvertParams

svg_path = sys.argv[1]; label = sys.argv[2] if len(sys.argv) > 2 else ""
data = Path(svg_path).read_bytes()
_, w, h, wrn = imaging.parse_svg_vectors(data)
print(f"SVG dims={w:.0f}x{h:.0f} bytes={len(data)} warnings={wrn}", flush=True)
params = ConvertParams(
    methods=["contour"], contour_simplify=1.5, curve_smooth=1,
    linemerge_tolerance_mm=0.5, linesimplify_tolerance_mm=0.1,
    linesort=True, reloop_tolerance_mm=0.05,
    page={"size": "A4", "orientation": "portrait", "margin_mm": 10,
          "padding_mm": 5, "frame": False, "frame_radius_mm": 2},
    label={"enabled": bool(label), "text": label, "align": "right",
           "height_mm": 5, "font": "futural", "border": True,
           "border_radius_mm": 2, "pad_left_mm": 0, "pad_right_mm": 0},
    pen={"draw_speed_mm_s": 40, "travel_speed_mm_s": 100, "pen_lift_s": 0.3},
    line_color="black", background="none",
)
pr = cProfile.Profile(); pr.enable()
t0 = time.perf_counter()
res = run_convert(image_id="f"*64, image_bytes=data, is_vector=True,
                  src_w=float(w), src_h=float(h), params=params, settings=Settings())
print(f"TOTAL_SECONDS={time.perf_counter()-t0:.3f}", flush=True)
print(f"strokes={res.stats.strokes} points_before={res.stats.points.before} "
      f"points_after={res.stats.points.after} segs_before={res.stats.segments.before} "
      f"segs_after={res.stats.segments.after} pen_down_mm={res.stats.pen_down_mm}", flush=True)
pr.disable()
s = io.StringIO(); pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(25)
print(s.getvalue())
```

### Commands

```sh
# container workdir is /srv/fund-vista; backend is a package under it
docker cp /tmp/profile_convert.py fundvista:/tmp/profile_convert.py
docker exec fundvista sh -c \
  'cd /srv/fund-vista && PYTHONPATH=/srv/fund-vista \
   python3 -u /tmp/profile_convert.py /data/images/<sha256>.svg Budapest' \
  > convert_profile.out 2>&1
# or in background for the long case:
docker exec fundvista sh -c \
  'cd /srv/fund-vista && PYTHONPATH=/srv/fund-vista \
   nohup python3 -u /tmp/profile_convert.py <big-svg> Budapest \
   > /tmp/conv_big.out 2>&1 & echo $!'
```

Saved map SVGs live in `/data/images/*.svg` (content-addressed). The ones
measured here (`ae2235f…`, `dfc5c01…`) were real user renders from 2026-09-23.

Stage timings are also available on live traffic without a profiler: the
pipeline logs `pipeline.convert image=… stage=… ms=…` at DEBUG, so run
uvicorn/app with `logging.basicConfig(level=logging.DEBUG)` (or a filter) to
get a per-stage waterfall with zero instrumentation.

## 3. CPU saturation measurement

Measured on the NAS host during a live convert, via the container's cgroup
`cpuacct` and docker stats:

```
# 10 s window while a convert was pegged
before=3353103531370  after=3362779257997  → 9.68 CPU-seconds / 10 s wall  (≈97% of one core)
docker stats fundvista  →  103.14%    (single convert)
host top → the python worker at %CPU 100.0, state R

# during the big-map run (parse + linemerge): 16.9 CPU-seconds / 10 s (≈1.7 cores,
# our 1 core + a second live convert that was already running)
```

So: **a convert uses ≈1 core**, the remaining `--cpus=3 − 1` is untouched while
one request converts. Multiple concurrent converts still add (the cgroup quota
caps the total), but the per-request latency is purely determined by the O(n²)
pure-Python loop, not by contention.

## 4. Hotspots (with code references)

All in `backend/penplot/`:

| Function | Cost on big map | Why |
|---|---|---|
| `optimize.py:66 linemerge` | **> 5 min** | greedy `while changed` + per-extension full `for j in work` scan; O(passes · n²) with `dist`/`math.hypot` per candidate. 1.49 M `math.hypot` calls on the *medium* map alone. |
| `optimize.py:215 linesort` | ~1.4 s (medium) | greedy nearest-neighbour: `while remaining` × `for j in remaining` → O(n²) `dist`. |
| `optimize.py:241 reloop` | ~1.4 s (medium) | `min(range(len(body)), key=…)` per closed ring, Python lambda overhead. |
| `imaging.py` `parse_svg_vectors` (`_flatten_path_d`, `_walk_children`) | ~8 s (big) | expat + per-shape/python flattening; re-run on every convert. |
| `optimize.py:30/271` `polyline_length`/stats | O(points) | python loop; minor on current sizes, grows with resolution. |

Nothing raster took measurable time on the designer path: methods are skipped
for vectors (`pipeline.py:126-133`).

## 5. Implications for the fix plan

1. **Cacheable by construction.** Result filenames already encode
   `{image_id}_{params_hash}_optimized.svg` (`pipeline.py:302`) and content
   addressing makes inputs immutable — repeat converts of identical
   (image, params) currently recompute from scratch instead of re-serving.
   A deterministic result+stats cache is the cheapest 5-minute → <10 ms win.
2. **`linemerge` / `linesort` must stop being O(n²).** Spatial bucketing on the
   quantised endpoints turns both into near-linear; output must stay
   byte-identical for identical inputs (deterministic tie-breaking).
3. **Stages can be skipped for previews.** `linesort` only orders pen travel
   (never geometry); a "preview/fast" mode that skips it (and heavy merges)
   turns the long tail into seconds.
4. **Parsed geometry is re-derivable.** `parse_svg_vectors` is 8 s of the big
   map; caching per content-hash (or reusing the render side's already-split
   layer geometry) removes it.
5. **CPU ceiling is not the constraint.** Fixes should target *work*, not
   parallelism; a `multiprocessing` worker would add capacity but not latency
   — the pipeline must just do less.

## 6. Planned phases (GH issues)

Phased in this repo's issues, each phase carries its own tests (unit/spec
+ performance guard) and DoD:

1. **Convert result cache** — re-serve `{image_id}_{params_hash}` results with
   persisted stats/vpype sidecar; identity and TTL semantics.
2. **Fast preview path** — optional/automatic stage skipping (`linesort` off,
   merge-light) for interactive slider feedback on large maps.
3. **`linemerge` spatial-index rewrite** — byte-identical output, near-linear
   cost, perf regression gate.
4. **`linesort`/`reloop`/stats vectorisation** — numpy neighbourhood index
   + batch stats; byte-identical output, perf regression gate.
5. **Parsed-geometry + layer caching** — cache `parse_svg_vectors` output by
   content hash; reuse render-side layer split for re-converts/imports.