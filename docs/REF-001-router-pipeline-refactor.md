# REF-001 — Router/pipeline refactor plan (fix every bug once)

Status: PLAN (accepted 2026-10-04). Work items land as separate branches/PRs.

## 0. Why this exists

Refactor priority = file length × commits touching it (114-commit history).
The same two domains top the ranking in **both** repos — and the owner
reported the failure mode this plan fixes: a furniture-overlap bug fixed
for citymap while the same bug class survived at airports, i.e. one bug
fixed in two places (or in one place only).

fund-vista backend (code files):

| # | File | Lines | Touches | Score |
|---|---|---|---|---|
| 1 | `backend/airports/router.py` | 714 | 8 | 5712 |
| 2 | `backend/citymap/router.py` | 790 | 7 | 5530 |
| 3 | `backend/airports/render.py` | 574 | 6 | 3444 |
| 4 | `backend/penplot/schemas.py` | 350 | 7 | 2450 |
| 5 | `backend/penplot/pipeline.py` | 406 | 6 | 2436 |
| 6 | `backend/penplot/imaging.py` | 1118 | 2 | 2236 |
| 7 | `backend/penplot/optimize.py` | 536 | 4 | 2144 |
| 8 | `backend/penplot/router.py` | 505 | 4 | 2020 |

pen-pixel-shop (excl. the fund-vista submodule):

| # | File | Lines | Touches | Score |
|---|---|---|---|---|
| 1 | `src/components/custom/CitymapTab.tsx` | 830 | 59 | 48970 |
| 2 | `src/components/custom/AirportTab.tsx` | 613 | 35 | 21455 |
| 3 | `src/lib/shop.server.ts` | 868 | 16 | 13888 |
| 4 | `src/lib/penplot.api.ts` | 690 | 15 | 10350 |
| 5 | `src/lib/shop.functions.ts` | 612 | 16 | 9792 |

(`sidebar.tsx` 745 lines × 2 touches and `imaging.py` 1118 × 2 are long
but stable — review only when touched, not refactor targets.)

## 1. Principles (owner constraints, non-negotiable)

1. **Citymap and airports are NOT the same.** Domain logic (bbox/Overpass
   vs ICAO/OurAirports, framing, layers) stays in domain modules. No
   cross-domain unification, no shared "generic renderer".
2. **Shared machinery is extracted so each bug is fixed once.** Cache
   sidecars, cancel checkpoints, thread-build discipline, furniture
   knockout zones: one helper + shared tests, both callers covered.
3. **Every fix ships with a regression test on the art that was broken**
   (citymap art for citymap bugs, airports art for airports bugs) — plus
   the shared-helper unit test where one exists.
4. Refactor steps are pure moves (no behavior change); suites stay green.

## 2. Work items (in order)

### A. Land the furniture-knockout branch (unblocks everything below)

`origin/feat/stats-table-exclusion-zone` (`89e355e` stats-table zone +
`c94925d` label knockout, `pipeline.py` + `labels.py` + `stats_table.py`)
was never merged and predates the bearing feature — it even deletes
`test_citymap_bearing.py`, so it needs a rebase/port onto current `main`,
not a merge. Land via the normal branch/PR flow with the full suite green.

### B. Airports context-table overlap (the reported double-fix)

Reproduce on current `main`: airports "context" streets overplotting the
info table. Cover with the **shared** knockout helper from (A) — extend
zone planning only if the airports table geometry differs from the citymap
one. Regression test renders **airports** art (the 90°-symmetric citymap
bearing tests proved art-specific tests are the only ones that catch
directional bugs). Done = one helper, both diagrams clean, no second fix.

### C. Router extraction, phase 1 (the two score leaders)

- `citymap/router.py` (790): extract `fetching.py` (`_fetch_missing`,
  `_load_raw`, `_select_layer`) and `render_cache.py` (split/render keys,
  cache load/store, `_tile_key`, `_count_paths_in_svg`). Router keeps
  endpoints + `_resolve_area` + `_error`.
- `airports/router.py` (714): same split (`_lookup`, `_load_polygons`,
  radius bucketing → fetching; split/render keys + cache → render cache;
  `_render_uncached`/`_build_svg` pipeline shared by its
  `render`/`import_diagram` pair, mirroring citymap).
- Dedupe `_fnum` (exists in both `airports/router.py:164` and
  `airports/render.py:75`); co-locate `render_diagram_version()` with its
  renderer as citymap already does. Pure moves, suites green.

### D. Shop tabs (deferred until A–C land)

`CitymapTab.tsx` (830 × 59) and `AirportTab.tsx` (613 × 35) dwarf
everything else in shop churn. `CitymapTab` already extracted
`useCitymapRender`; `AirportTab` should mirror that hook pattern rather
than grow its own. Take up only after the backend phases are done.

## 3. Non-goals

- Unifying citymap + airports domain logic (see §1.1).
- Behavior changes inside refactor steps.
- Shop auto-deploy impact: A–C are fund-vista backend only (NAS manual
  deploy); D is shop (auto-deploy) and stays separate.

## 4. Acceptance

- (A): branch rebased, suite green, merged; production renders show
  knockout for citymap art.
- (B): airports overlap reproduced → gone, with an airports-art regression
  test; no citymap-only fix accepted.
- (C): both routers under ~400 lines, no duplicated pipeline, suites green.
- (D): separate ticket after A–C.
