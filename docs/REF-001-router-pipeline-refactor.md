# REF-001 — Router/pipeline refactor plan (fix every bug once)

Status: PLAN (accepted 2026-10-04; reviewed 2026-10-05 against
post-merge numbers — §5). Work items land as separate branches/PRs.

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
| 4 | `src/lib/penplot.api.ts` | 690 | 16 | 11040 |
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

## 5. Review 2026-10-05 (post-merge numbers)

Recomputed after the merge queue (#35 fund-vista; #869, #866, #859, #848,
#858 shop — 6 merges). Rankings are unchanged in both repos: the two
routers still lead fund-vista, the two designer tabs still lead shop by an
order of magnitude (`CitymapTab` 830 × 59, `AirportTab` 613 × 35).
`penplot.api.ts` moved 15 → 16 touches; nothing else in either top-5 moved.

The queue itself validated the plan's premise (§1.2) three times over:

- **TC-D33 claimed twice.** `#834` (preview fit) and the layer-framing
  work both shipped a "TC-D33"; the later merges re-homed preview fit to
  the free **TC-D34** (spec + doc row + mapping + execution-order refs).
  Lesson: TC numbers are a shared namespace — check the free list before
  writing the spec, not at merge time.
- **One component, three authors.** `PageHeader` drew a `<header>` (#836),
  an `<hgroup>` inner (#850), and a no-local-`.blueprint` rule (TC-X4)
  from three branches; the merge keeps all three, and TC-X4's chrome
  assertion is now scoped to the direct child (`:scope > header`) so
  semantic title headers inside `<main>` stop tripping it.
- **Stale-tree CI failures are environmental until proven otherwise.**
  Twice a hermetic lane failed on a tree that was green on re-run with a
  clean build (`reuseExistingServer: true` + old `.output`, stale `~/pps`
  without `rm -rf`); e2e also MUST NOT run on the dev Mac (no browsers,
  wrong server on :5173, policy `docs/e2e-environment.md` §Mac-local).
  Diagnose from the job log first; never "verify" against the dev Mac.

Open after the queue (NOT merged — red CI, owner decision pending):

- `#844` (preview fit phase 1): red hermetic lane (#134, logs
  unauthenticated from here); content superseded by merged #858 — close
  as superseded or re-run CI to confirm.
- `#852` (#845 phone viewport): red hermetic lane; overlaps #858's merged
  phone cases — same treatment.
