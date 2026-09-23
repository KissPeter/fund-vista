# CR-002 Convert pipeline performance (P1–P5)

- Status: done
- Created: 2026-09-23
- Last updated: 2026-09-23

Fix the `POST /v1/convert` hang on dense city maps: a 76k-point SVG never
left `linemerge` (> 5 min, one pegged core), and the shop reported repeated
"convert hangs for 5 minutes" on large saved maps. Profiling record:
`docs/convert-performance.md`. Plan + tracker: GH issues #26–#30.

## Phases

- [x] **P1 — Convert result cache** (#26): `/v1/convert` re-serves
  `{image_id}_{params_hash}_optimized.svg` from a persisted stats/warnings/
  vpype sidecar with no geometry work; TTL + identity semantics. Landing:
  `57ee556`. Tests: `test_penplot_preview.py` (HTTP identity),
  `test_penplot_store.py` (sidecar round-trip + < 10 ms read guard).
- [x] **P2 — Fast vector preview path** (#27): vector previews skip
  `linesort`/`reloop` unless `full_quality`, emit `travel_optimization_off`
  warning; `vpype_command` recipe reflects what ran; raster-only params leave
  the vector result signature. Landing: `57ee556`.
- [x] **P3 — `linemerge` spatial-index rewrite** (#28): single-pass merge over
  a tol-sized spatial hash with a 5×5 neighbourhood (exact distance filter);
  byte-identical selection. Implementation landed with `13d1978`; the frozen
  reference-equivalence + perf gate ships in `9cbcd88` (`test_penplot_optimize.py`).
- [x] **P4 — `linesort`/`reloop`/stats vectorisation** (#29): spatial-hash
  `linesort` (Chebyshev ring + exact linear fallback, `13d1978`); numpy `reloop`
  (`np.argmin`) + `polyline_length` (`9cbcd88`) — byte-identical (tie/symmetric-ring
  fixtures + perf guards).
- [x] **P5 — Parsed-geometry + layer caching** (#30): part A parsed-SVG disk
  cache (`svgparsecache.py`, 48 h lazy TTL, atomic writes); part B citymap/
  airports split-geometry Redis cache keyed on exact sorted layer set / Overpass
  payload (per-layer keys unsound: `match_way_layer` reassigns). Landings:
  `57ee556` + `30e3c04`. Exercised by `test_jobs_contract.py` + existing
  citymap/airports suites.

## Definition of Done

- [x] Code rules: settings-only env (`parsed_svg_ttl_hours`), Pydantic v2, no
  new deps (numpy already present), routes already registered.
- [x] TDD red → green → refactor: reference implementations frozen BEFORE the
  rewrites; equivalence fixtures added with the code.
- [x] Test-type selection: live-server HTTP for cache identity + preview
  contract (`test_penplot_preview.py`); unit for algorithms
  (`test_penplot_optimize.py`); timing guards in both.
- [x] Full suite green: 313 passed (baseline 279) via `./runtests.sh`.
- [x] Docs + Serena memory synced: `convert-performance.md` §7, this item,
  deploy gate updated.
- [x] Commit per phase: `57ee556` (P1/P2/P5A), `13d1978`+`9cbcd88` (P3/P4),
  `30e3c04` (P5B).
- [x] Push only when green: pushed to `main` → NAS rebuild auto-trigger
  (see `.opencode/skills/deploy-nas/`).
- [x] GH issues #26–#30 closed as each landed.