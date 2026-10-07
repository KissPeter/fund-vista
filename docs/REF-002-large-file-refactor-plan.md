# REF-002 — Large-file refactor plan (files > 500 lines, PLAN only)

- Status: PLAN (no code changed in this ticket)
- Created: 2026-10-06
- Scope: `backend/**/*.py` + `src/**/*.{ts,tsx}`, > 500 lines; vendored,
  generated, and lockfiles excluded
- Method: `wc -l` inventory + `git log --since="2026-09-15" --oneline -- <file>`
  (21-day window ending 2026-10-06), most-churned first
- Constraint: pure restructuring only — split files, extract
  modules/functions, deduplicate — identical behavior, no functional change
- Relation to REF-001: REF-001 (`docs/REF-001-router-pipeline-refactor.md`)
  already ranks by length × touches and proposes router/pipeline extraction
  with the fix-once rule. This plan is the issue-#45 inventory: full >500-line
  sweep (backend + frontend), 21-day churn ranking, per-file findings, lint
  notes, and sequenced phases. Where it overlaps REF-001 (the two routers,
  `_fnum`, cache sidecars) it reuses REF-001's direction instead of
  inventing a second one.

## 0. Inventory method (reproducible)

```sh
# backend candidates (production code; tests listed separately in §2)
find backend -name '*.py' -not -path '*__pycache__*' \
  | while read f; do echo "$(wc -l < "$f") $f"; done | sort -rn
# frontend candidates
find src -type f \( -name '*.ts' -o -name '*.tsx' \) -exec wc -l {} + | sort -rn
# churn per file (21 days back from 2026-10-06)
git log --since="2026-09-15" --oneline -- <file> | wc -l
```

Exclusions applied: `src/components/ui/*` (shadcn vendored — see §3),
`dist/`, lockfiles (`bun.lockb`, `package-lock.json`, `backend/uv.lock`),
`__pycache__`, binary assets (e.g. `*.ttf`).

File-level eslint probe used below: `bun x eslint <file>` (repo `lint`
script is `eslint .`; a full-tree run currently fails inside the eslint
`debug`/`@babel` resolution on this box, so per-file runs are the evidence
quoted). Backend has no Ruff config in this repo (`backend/pyproject.toml`
holds only project metadata + deps) and no Python toolchain on this box, so
"Ruff issues" below are the classes Ruff would flag (broad `except`,
`type: ignore`, long lines, complexity/duplication), spotted by inspection —
every phase re-runs the real linters as a gate (§5).

## 1. Churn ranking (past 21 days, most-churned first)

Counts are commits touching the file since 2026-09-15 (`main` + merged PRs
#32–#44; window holds 51 commits, 2026-09-16 → 2026-10-05).

| # | File | Lines | Churn (21d) | Notes |
|---|------|------:|------------:|-------|
| 1 | `backend/citymap/router.py` | 790 | 5 | top churn; framing/bearing/cache/cancel |
| 2 | `backend/airports/router.py` | 714 | 3 | tie-break: longest of the 3s, widest blast radius |
| 3 | `backend/penplot/router.py` | 534 | 3 | result-meta endpoint (10-05) + cancel + sidecar cache |
| 4 | `backend/penplot/optimize.py` | 536 | 3 | spatial-index + numpy perf passes (09-23) |
| 5 | `backend/penplot/methods.py` | 683 | 1 | NumPy-2 centerline fix only; stable otherwise |
| 6 | `backend/penplot/imaging.py` | 1118 | 1 | longest file; svg-sniff fix only; stable |
| 7 | `backend/jobs/runner.py` | 636 | 1 | single P2/P3 creation commit; stable since |
| 8 | `backend/airports/render.py` | 574 | 1 | frame-fit/clip fix only; stable |
| 9 | `src/pages/Index.tsx` | 693 | 0 | no commits in window; frontend churn lives elsewhere |
| — | `src/components/ui/sidebar.tsx` | 761 | 0 | EXCLUDED — vendored shadcn (see §3) |
| — | `backend/tests/test_airports.py` | 882 | 2 | test code: do not split (regression cover) |
| — | `backend/tests/test_citymap.py` | 510 | 2 | test code: do not split (regression cover) |

Tie-break rule: churn first; ties broken by lines (larger first), then by
blast radius (router > pipeline helper). The ranking sets the phase order:
routers first (§6 Phase 1–2), then jobs runner (Phase 3), then penplot
helpers (Phase 4), frontend last (Phase 5).

## 2. Per-file findings

### 2.1 `backend/citymap/router.py` — 790 lines, churn 5 (rank 1)

- What: `/v1/citymap/*` endpoints (`geocode`, `geocode/search`, `render`,
  `import_map`, `results`, `layers`) + area resolution, tile/split/render
  cache keys, Overpass fetch-or-cache, layer selection, SVG path counting.
- Why it grew: five feature/perf commits in-window stacked independent
  concerns into one module — tile-granular OSM cache + snapped bboxes (P5),
  cooperative cancel P2 (4 checkpoints duplicated across `render` and
  `import_map`), parsed-geometry cache P5B, picker-bearing render, stable
  north-up framing. Each commit added a helper + an endpoint branch.
- Duplication shared: `_error(status, code, message)` is byte-identical in
  `airports/router.py:75` (and a third variant `_error_response` in
  `penplot/router.py:113`, plus `jobs/router.py:50`); cache
  load/store/key helpers mirror `airports/router.py` (`_split_key(s)` /
  `_render_keys` / `_load_cached_*` / `_store_*`); P2 checkpoint
  try/except blocks are copy-pasted between `render` and `import_map`
  inside this same file; `from backend.penplot.router import store` couples
  both map routers to the penplot result store.
- Ruff classes spotted: no lines > 120 (max 114); no bare `except:`; one
  `noqa` none; main risk is complexity/duplication (C901-style: `render`
  ~110 lines, `import_map` ~110 lines, `_load_raw` ~85 lines) and the
  `import … inside function` cancel imports per endpoint. Fix as part of
  refactor: hoist imports, extract `fetching.py` + `render_cache.py` so no
  function exceeds ~60 lines, keep `ruff check` clean.

### 2.2 `backend/airports/router.py` — 714 lines, churn 3 (rank 2)

- What: `/v1/airports/*` endpoints (`search`, `lookup`, `render`,
  `import_diagram`, `results`) + OurAirports lookup, polygon loading, radius
  bucketing, split/render cache keys, `_render_uncached`/`_build_svg`
  pipeline shared by `render` and `import_diagram`.
- Why it grew: same P2/P3/P5 stack as citymap (cancel checkpoints, jobs
  reuse, geometry cache) plus domain extras (runway infos, frequency rows,
  radius bucketing). Already partially factored (`_render_uncached`
  extracted) but the two endpoints still duplicate validation/cache/checkpoint
  scaffolding (~70 lines each).
- Duplication shared: `_fnum` defined here (`:164`) AND in
  `airports/render.py:75` (identical 5-line helper — dedupe to one home,
  e.g. `airports/geometry.py` or a `_num.py`); `_error` identical to
  citymap's; cache helpers mirror citymap's with `airports_cache_key`;
  `_render_uncached` is additionally called from `jobs/runner.py`
  (`_exec_airport_render/import`), so its signature is a three-caller
  contract — extract carefully, keep signature stable.
- Ruff classes spotted: 5× `type: ignore[arg-type]` on `runways=` /
  `frequencies=` / `query=` (schema-type friction — fix types instead of
  ignoring where the split touches them); 2× broad `except Exception`
  around cache reads (`:291`, `:298` — narrow to
  `(RedisError, ValueError)` or keep with a comment + log); max line 100,
  clean. Fix as part of refactor: remove at least the touched ignores,
  narrow the cache `except`s.

### 2.3 `backend/penplot/router.py` — 534 lines, churn 3 (rank 3)

- What: `/v1/*` image endpoints (`upload`, `convert`, `results`,
  `results/{filename}/meta`, `tokens`, `retain`, `health`) + rate limiting,
  validation, result-sidecar fast path, public-base resolution.
- Why it grew: convert-result sidecar cache P1 (fast path ~30 lines inside
  `convert`), result-meta endpoint (newest commit), P2 checkpoints, token
  mint/verify. `convert` (~120 lines) is the long function; the rest is one
  thin endpoint per ~30 lines — healthy shape, just too many in one file.
- Duplication shared: `_error_response` ≈ the two map routers' `_error`;
  `resolve_public_base` / `_client_ip` / `require_rate_limit` are imported by
  map routers (already shared — keep); `store` (`ImageStore`) is imported BY
  both map routers, so this file is a dependency hub — splits must not create
  import cycles (extract outward: handlers import store, never the reverse).
- Ruff classes spotted: 2× broad `except Exception` (`:140` meta rebuild,
  `:518` retain path — both deliberate fallbacks; narrow or comment);
  max line 97. Fix as part of refactor: extract `convert.py` /
  `images.py` / `tokens.py` handler modules, keep `router.py` as wiring only.

### 2.4 `backend/penplot/optimize.py` — 536 lines, churn 3 (rank 4)

- What: pure-Python vpype-equivalent stage chain (`layout → quantize →
  linemerge → linesimplify → linesort → reloop → to_svg`) +
  `build_vpype_command` descriptor + page-size/layout math.
- Why it grew: three 09-23 perf commits (spatial-index merge, numpy
  sort/reloop/stats, sidecar cache) each added a stage variant + stats
  plumbing in place. Cohesive but long: 12 public stage functions + 4
  private helpers + layout/svg builders.
- Duplication shared: `cell()` grid helper defined twice in-file
  (`linemerge:89`, `linesort:356` — parameterize into one `_grid_cell`);
  `dist`/`polyline_length`/`count_points` overlap conceptually with
  `methods.py` path-length helpers (`_drop_echo_rails.path_len`,
  `CenterlineMethod` tracing lengths); `layout`/`layout_scale`/`page_dims_mm`
  form a page-math cluster also touched by `stats_table.py`/`labels.py`
  (REF-001 knockout work). No cross-file copy-paste — cluster by stage.
- Ruff classes spotted: 1 line > 120 (`layout_scale` signature, 158 chars —
  wrap it); pure functions, no `except`, no ignores. Lowest-risk split in
  the plan (pure moves + unit tests).

### 2.5 `backend/penplot/methods.py` — 683 lines, churn 1 (rank 5)

- What: `LineMethod` protocol + 4 method classes (`Contour`, `Hatch`,
  `Flow`, `Centerline`) + Zhang-Suen thinning + skeleton
  collapse/degree/prune/trace + echo-rail drop.
- Why it grew: four independent algorithms + their private helpers
  (~250 lines of skeleton machinery) behind one protocol. Only one
  in-window commit (NumPy-2 bool-mask fix) — stable, but every future
  method edit (e.g. a `PotraceMethod` per the module docstring) lands in
  this file and risks conflicts.
- Duplication shared: skeleton degree/prune/neighbor helpers are used only
  by `CenterlineMethod` (co-locate, don't generalize); `path_len`/`bbox`
  closures in `_drop_echo_rails` duplicate `optimize.polyline_length`
  semantics in pixel vs mm units — document the unit boundary, don't merge.
- Ruff classes spotted: max line 101, no broad except, no ignores. Split is
  one-class-per-module; the protocol + `MethodContext` stay in `methods.py`.

### 2.6 `backend/penplot/imaging.py` — 1118 lines, churn 1 (rank 6)

- What: two halves — raster ops (`looks_like_svg`, `sniff_extension`,
  `load_raster`, `probe_raster`, downscale/blur/contrast/brightness/
  background/threshold/hatch-strip, `a4_dpi`) + a full SVG vector pipeline
  (`_parse_transform`, viewBox CTM, shape walker, path-`d` flattener with
  arc/cubic/quad sampling ~230 lines, text runs, Hershey fallback).
- Why it grew: the SVG half (~800 lines) is a self-contained vector
  renderer that accreted inside a raster module (path grammar + transform
  stack + text handling). Longest file in the repo but churn 1 — stable
  because nobody touches it, risky for the same reason (no owner, big
  blast radius on `convert`).
- Duplication shared: `_first_number`/`_parse_length`/`_style_decls` overlap
  ad-hoc SVG parsing that `svgfont.py` also does for the ZnikoSL face
  (check before splitting — share a `_svg_parse.py` only if the grammars
  truly match, else keep separate with a comment); matrix helpers
  (`_mmul`/`_mapply`) are local-only.
- Ruff classes spotted: 2 lines > 120 (max 130); 3× broad
  `except Exception` (`:74` sniff fallback, `:1023`/`:1031` Pillow fallbacks
  — all deliberate; narrow to `(UnidentifiedImageError, ValueError)` etc.
  where the split touches them). Fix as part of refactor: wrap long lines,
  narrow the three `except`s, split raster vs vector halves.

### 2.7 `backend/jobs/runner.py` — 636 lines, churn 1 (rank 7)

- What: `/v1/jobs` background execution — task map, cancel poller, `_execute`
  dispatcher, 5 `_exec_*` handlers (citymap render/import, airport
  render/import, convert), job-exception store, timeout path.
- Why it grew: single P2/P3 creation commit fused 5 endpoint re-executions
  + infra (polling, timeouts, error mapping) into one file. Each `_exec_*`
  is 60–85 lines; the file is a 5× repetition of
  validate → cancel-check → delegate-to-router-helper → store.
- Duplication shared: `_exec_citymap_render` vs `_exec_citymap_import`
  (~80 lines each, differ only in delegate call + endpoint string); same
  pair for airports (both call `_render_uncached`); `_raise_if_cancelled`
  + `cancelled()` closure pattern repeats per handler;
  `request=req, endpoint=…, started_mono=…  # type: ignore[arg-type]` ×4
  (`:198`, `:274`, `:364`, `:443`); `_JobError`/`_job_err` duplicate the
  routers' `_error` shape. Split per domain AFTER routers expose stable
  `*_uncached` delegates (dependency on Phase 2).
- Ruff classes spotted: 4× `type: ignore[arg-type]` (the `_JobRequest`
  shape vs router handler signatures — fix with a typed delegate signature
  during the split); 4× broad `except Exception` in queue/poller paths
  (`:54`, `:114`–`:125`, `:541`, `:563` — keep for robustness but log);
  `noqa: D102` on the fake-request stub (fine).

### 2.8 `backend/airports/render.py` — 574 lines, churn 1 (rank 8)

- What: pure airfield SVG renderer (`render_diagram`: project → clip →
  frame → emit) with nested closures (`proj`, `_project_polys`, `W2S`,
  outcode/segment/edge/closed clip chain, frame test, path builder).
- Why it grew: projection + Cohen–Sutherland clipping + frame math +
  emission all closed over one frame scope. Single in-window commit
  (frame-fit/clip fix) — stable, cohesive, hardest to split safely.
- Duplication shared: `_fnum` duplicate (see §2.2 — the one-line fix both
  phases assume); `_esc` vs other XML-escaping (check `svgfont.py`/
  `stats_table.py` for a shared escape before generalizing); clip helpers
  are renderer-local (do not extract into a "generic clipper" — REF-001
  principle §1.1: domain geometry stays in domain modules).
- Ruff classes spotted: `import inspect`, `import sys` at top (verify used —
  `ruff check` flags unused imports; remove if dead during split);
  `lru_cache` + `hashlib` versioning is load-bearing (keep);
  1× broad `except Exception` (`:124`); 1× `type: ignore[arg-type]` (`:79`,
  same `_fnum` — disappears with the dedupe). Minimal-touch phase:
  hoist closures to module functions, extract at most `clip.py`, keep pixel
  output byte-identical (SVG golden test).

### 2.9 `src/pages/Index.tsx` — 693 lines, churn 0 (rank 9)

- What: fund-vista dashboard page — provider switch, fund list + search +
  currency filter, yield loading with progress + caches, fund-detail chart,
  top-gainer + investment-analysis actions, privacy banner, 3-tab layout
  (`funds`/`analysis`/`investments`) composing `FundCard`,
  `InvestmentChart`, `InvestmentsTab`, `DateRangeFilter`.
- Why it grew: 16 `useState`s + 5 `useEffect`s + 7 handlers
  (`loadFunds`, `loadYieldData` ~125 lines, `handleFundClick` ~80 lines,
  `handleRangeChange`, `handleFindTopGainer`, `handleAnalyzeInvestmentFund`,
  `dismissPrivacyBanner`) + ~250 lines of JSX in one component. Zero
  in-window commits — not churny, but the largest frontend file and the only
  non-vendored one > 500 lines, so any future dashboard work lands here.
- Duplication shared: yield-cache logic (success/failure caches with expiry)
  inside `loadYieldData` is self-contained (~60 lines — extract to a hook,
  no other consumer yet); filter/sort (`filteredFunds`, `sortedFunds`,
  `currencyButtons`) mirrors table-filter patterns in `InvestmentsTab`
  (405 lines — share only the predicate type, not the UI); API shapes come
  from `services/investmentApi.ts` (396 lines, just under the bar — do NOT
  split it in this plan, note as watchlist).
- eslint classes spotted: file-level `bun x eslint` is CLEAN (exit 0 — the
  repo disables `@typescript-eslint/no-unused-vars`, so unused imports
  wouldn't flag; check manually during split). Manual notes for the phase:
  ~14 `console.log`/`console.error` debug lines with emoji prefixes
  (`:147`, `:176`–`:259`, `:364`, `:417` — route through a `logger` or
  delete); 5 `useEffect`s need exhaustive-deps review on extraction;
  `strict: false` + `noImplicitAny: false` in `tsconfig.app.json` mean the
  split must not rely on `tsc` to catch shape drift — add explicit prop
  types on every extracted component/hook. Full-tree `eslint .` currently
  fails on this box for environmental reasons (§0) — the phase must still
  gate per-file eslint + `tsc --noEmit` + `vite build` green.

### 2.10 Watchlist (just under the bar — do NOT split in this plan)

`src/components/InvestmentsTab.tsx` (405), `src/services/investmentApi.ts`
(396), `src/components/ui/chart.tsx` (363, shadcn), `src/components/
InvestmentChart.tsx` (337). Re-check after Phase 5; if any crosses 500,
file the follow-up then.

## 3. Explicitly excluded (not candidates)

- `src/components/ui/sidebar.tsx` (761 lines, churn 0): standard shadcn
  sidebar — `cva` variants, `Slot`, `Sheet`, `Tooltip` composition. Vendored
  component pattern (matches `components.json` + every other `ui/*` file);
  splitting it forks upstream and breaks future shadcn updates. Decision:
  EXCLUDE from all phases; never edit except via the shadcn CLI.
- Lockfiles / generated: `bun.lockb`, `package-lock.json`,
  `backend/uv.lock`, `dist/*` (eslint-ignored), `__pycache__`.
- Binary: `backend/penplot/fonts/Znikoslsvginot8-GOB3y.ttf`.
- Test files over 500 lines (`test_airports.py` 882, `test_citymap.py` 510):
  counted in §1 for honesty, but NOT split — they are the regression cover
  the phases rely on. If a phase needs new coverage, it ADDS focused test
  files; it never restructures these two.

## 4. Principles (binds every phase)

1. No functional change: identical routes, payloads, SVG pixels, cache keys,
   and status codes. Splits are pure moves; any behavior fix found
   mid-split is filed separately, not snuck in.
2. Churn order: highest-churn files split first (routers), stable giants
   (`imaging.py`) split last among backend, frontend last overall.
3. Fix-once (from REF-001 §1): shared helpers (`_error`, `_fnum`, cache
   sidecars, cancel checkpoints) get ONE home + shared tests; both callers
   use it. No cross-domain unification beyond that.
4. Each phase merges before the next starts; a later phase names the earlier
   phase it builds on. Each phase is independently implementable, reviewable,
   and verifiable (§6), and each pairs implementation with review: the
   implementation PR is reviewed before merge, the review may make small
   code fixes itself, and work continues only after the previous phase is
   merged.

## 5. Verification gates (every phase, no exceptions)

Backend (per `runtests.sh` — suite runs inside the `fundvista-dev`
container; rebuild the image after any `backend/pyproject.toml` change):

```sh
./runtests.sh            # full backend suite, must be green
./runtests.sh <touched-test-file> -q   # fast loop during the split
```

Plus: `ruff check backend/` (or the repo's configured Ruff gate if one
lands before the phase) clean on touched files; no new `type: ignore`,
`noqa`, or broad-`except` without a comment; byte-compare a sample SVG
before/after for render-touching phases (`render` output for a fixed
citymap bbox + airports ICAO + convert fixture must be identical).

Frontend (Phase 5): `bun x tsc --noEmit`, per-file + tree `eslint`,
`bun run build` (vite) green; related e2e specs on `testbox` per
`docs/e2e-environment.md` if the repo's e2e covers the dashboard, else a
manual before/after screenshot pair attached to the PR. (Note, recorded
2026-10-06: `tsc --noEmit` on this box reports pre-existing
`tsconfig.json(8,5): error TS5102: Option 'baseUrl' has been removed` —
the phase owner re-checks whether that predates the phase and either
fixes the config or records it, but does not ship a red gate.)

No-functional-change proof per phase: the phase PR lists the exact gates
run + sample-SVG hashes (backend) or build + screenshot/spec refs
(frontend). A phase with a red gate or an unexplained test failure does
not merge; failures are dispositioned per the repo DoD (fixed when the
phase caused them, otherwise recorded).

## 6. Phases (sequenced; each builds on the previous merge)

### Phase 1 — Router shared scaffolding (ranks 1 + 2 + 3)

Builds on: nothing (first phase; lays the foundation all later phases use).
What changes: extract the three routers' identical scaffolding into one home
(e.g. `backend/http/` or `backend/common/`): `_error` (from
`citymap/router.py:94` + `airports/router.py:75` + `penplot/router.py:113`
`_error_response`), cache get/set-many thin wrappers if they are truly
identical (otherwise leave domain key-builders in place and share only the
wrapper), and the cancel `log_and_499` import pattern (hoist the per-endpoint
function-level cancel imports to module level). Dedupe `_fnum` (both
airports files → single definition, re-export at the old sites or fix
imports). Fix the touched Ruff classes: remove dead `inspect`/`sys` imports
if `ruff` flags them, narrow the cache `except Exception`s, wrap >120-char
lines.
Files: `backend/citymap/router.py`, `backend/airports/router.py`,
`backend/penplot/router.py`, `backend/airports/render.py` (`_fnum` import
only), new `backend/http/_errors.py` (or equivalent) + unit test for it.
Verify: `./runtests.sh` full suite green; `ruff check` clean on touched
files; sample-SVG hashes unchanged (no render logic touched — hashes must
match by construction).
Review pairing: implementation PR → reviewer checks the shared module has
exactly one behavior (same status/code/message shapes), may push small
fixes (e.g. a missed call site) directly; merge only after suite + ruff
green; Phase 2 starts after this merge.

### Phase 2 — Router endpoint dedup (ranks 1 + 2; builds on Phase 1)

Builds on: Phase 1 (reuses the shared `_error`/cache wrappers; do not start
until Phase 1 is merged — the endpoint diffs conflict otherwise).
What changes: citymap — extract `fetching.py` (`_fetch_missing`,
`_load_raw`, `_select_layer`) and `render_cache.py` (split/render keys,
cache load/store, `_tile_key`, `_count_paths_in_svg`) so `render` and
`import_map` share one validated-fetch-render-store path (mirrors REF-001
§2.C); airports — same split (`_lookup`, `_load_polygons`, radius
bucketing → `fetching.py`; split/render keys + cache → `render_cache.py`;
keep `_render_uncached`/`_build_svg` as THE shared pipeline for
`render`/`import_diagram`, called unchanged by `jobs/runner.py`).
Target: both routers under ~400 lines. Pure moves; signatures of
`_render_uncached` (three callers) frozen.
Files: `backend/citymap/router.py` + 2 new modules, `backend/airports/
router.py` + 2 new modules. Verify: `./runtests.sh` green (esp.
`test_citymap.py`, `test_airports.py`, bearing/frame/tile tests);
sample-SVG hashes for a fixed bbox + ICAO identical; ruff clean.
Review pairing: implementation PR → reviewer renders the sample bbox/ICAO
before/after (or checks the posted hashes), may fix small naming/typing
issues in-PR; merge, then Phase 3.

### Phase 3 — Jobs runner per-domain split (rank 7; builds on Phase 2)

Builds on: Phase 2 (the `_exec_*` handlers delegate to router helpers —
splitting before the router delegates are stable doubles the merge
conflicts; wait for the Phase 2 merge).
What changes: split `backend/jobs/runner.py` (636) into
`runner.py` (dispatch + infra: task map, poller, `_execute`, `_run_job`,
error mapping) + `_exec_citymap.py` (render/import pair, one shared private
that takes the endpoint string) + `_exec_airports.py` (same pattern over
`_render_uncached`) + `_exec_convert.py`. Collapse the 4×
`type: ignore[arg-type]` with a typed delegate signature; keep the broad
queue/poller `except Exception`s but ensure each logs.
Files: `backend/jobs/runner.py` + 3 new modules. Verify: `./runtests.sh`
green (jobs/cancel/timeout tests red→green if touched); ruff clean; no
sample-SVG change (no renderer touched — hashes re-posted as a no-op proof).
Review pairing: implementation PR → reviewer checks each `_exec_*` pair
shares exactly one code path (no divergent validation), may push small
fixes; merge, then Phase 4.

### Phase 4 — Penplot helper splits (ranks 4 + 5 + 6; builds on Phase 3)

Builds on: Phase 3 (backend routing/jobs layout settled; these splits touch
only `convert`-adjacent pure helpers, so they go after the router churn to
avoid rebasing hot files — wait for the Phase 3 merge).
What changes, in file order (one PR per file, merged in sequence):
(a) `optimize.py` → `stages/` cluster (`merge.py`, `simplify.py`,
`sort.py`, `layout_svg.py` + shared `_grid.py` for the duplicated `cell()`);
wrap the 158-char `layout_scale` signature.
(b) `methods.py` → one module per class (`contour.py`, `hatch.py`,
`flow.py`, `centerline.py` + skeleton helpers co-located with centerline);
`methods.py` keeps `Protocol` + `MethodContext` + re-exports.
(c) `imaging.py` → `raster.py` (all raster ops) + `svg_vector.py` (transform
stack, shape walker, path flattener, text/Hershey); narrow the 3 broad
`except`s; share `_svg_parse.py` with `svgfont.py` ONLY if grammars match
(else a comment saying why not).
(d) `render.py` (airports, minimal touch): hoist the frame closures to
module functions, optionally extract `clip.py`; SVG golden test must pass
byte-identical.
Files: `backend/penplot/optimize.py`, `backend/penplot/methods.py`,
`backend/penplot/imaging.py`, `backend/airports/render.py` + new modules +
focused unit tests (stage golden tests, thinning fixtures, SVG-vector
fixtures). Verify per PR: `./runtests.sh` green; ruff clean; sample-SVG
hashes identical (this is THE pixel-proof phase — post convert/citymap/
airports hashes in each PR).
Review pairing: each file-PR is reviewed + merged before the next file-PR
starts (reviewer may push small fixes, e.g. a missed export); Phase 5 starts
after all four are merged.

### Phase 5 — Frontend dashboard split (rank 9; builds on Phase 4)

Builds on: Phase 4 (backend fully restructured and green; frontend goes
last so dashboard API shapes from `investmentApi.ts` are stable — wait for
the Phase 4 merge).
What changes: split `src/pages/Index.tsx` (693) into `Index.tsx` (tabs
layout + provider switch only, ~150 lines) + `hooks/useFunds.ts`
(list/load/filter state) + `hooks/useYields.ts` (yield cache + progress,
absorbing the ~60-line cache logic) + `hooks/useFundChart.ts`
(detail/chart/analysis actions) + `components/dashboard/` (filter bar,
fund grid, chart panel, privacy banner as presentational components with
explicit prop types). Delete-or-log the ~14 `console.*` debug lines;
review the 5 `useEffect` dep arrays on extraction; do NOT touch
`investmentApi.ts` (396, watchlist) or `ui/*` (vendored).
Files: `src/pages/Index.tsx` + ~6 new files, no backend files. Verify:
`bun x tsc --noEmit`, `eslint` (per-file + tree), `bun run build` green;
dashboard e2e/manual screenshot before/after pair; no API-shape change
(`investmentApi` untouched).
Review pairing: implementation PR → reviewer clicks through funds/
analysis/investments tabs + range/currency/provider switches against the
screenshots, may push small prop-type/console-cleanup fixes; merge closes
the plan (re-check the §2.10 watchlist and file follow-ups if anything
crossed 500).

## 7. What this plan does NOT do

- No behavior change in any phase (no new endpoints, params, layers,
  defaults, copy, or styling).
- No citymap/airports domain unification (REF-001 §1.1 stands).
- No `sidebar.tsx` / shadcn fork, no lockfile churn, no test-file
  restructuring, no `investmentApi.ts` split.
- No phase starts before the previous phase's PR is merged.

## Appendix — evidence commands (run 2026-10-06 on `agent/issue-45`)

- `wc -l` inventory (§0) produced the 8 + 1 + 1 candidate list.
- `git log --since="2026-09-15" --oneline -- <file>` produced the §1 counts
  (window: 51 commits, 2026-09-16 → 2026-10-05, PRs #32–#44).
- `bun x eslint src/pages/Index.tsx src/components/ui/sidebar.tsx` → exit 0
  (clean per-file); full `eslint .` fails on this box in `debug`/`@babel`
  resolution (environmental, recorded for Phase 5).
- `bun x tsc --noEmit` → pre-existing `tsconfig.json(8,5) TS5102 baseUrl`
  error (recorded for Phase 5).
- Backend suite (`./runtests.sh`, needs the `fundvista-dev` container) and
  `ruff` (no Python toolchain on this box) were NOT runnable here — every
  phase re-runs them as gates (§5).
