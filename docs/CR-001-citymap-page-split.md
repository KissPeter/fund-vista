# CR-001 — Split citymap out of `/penplot` into `/citymap`

- Status: done
- Created: 2026-09-13
- Last updated: 2026-09-13
- Type: change request
- Decided: standalone `GET /citymap` server page; MapLibre + OpenFreeMap
  preview; full removal of citymap from `/penplot`.

## Motivation

`/penplot` mixes two concerns: tweaking arbitrary uploaded images and
loading OSM city maps (section "2. City map (OpenStreetMap)" was
shoehorned in). The city-map flow deserves its own endpoint with search,
pan, zoom, and layer toggles, while `/penplot` goes back to image-only
tweaking.

Background: `docs/drawscape-two-phase-rendering.md` (two-phase
preview → render), `docs/hungary-europe-map-providers.md` (free tile
source), `docs/drawscape-map-source-findings.md` (layer mapping).

## Explicitly unchanged

- `/v1/citymap/*` JSON API (`geocode`, `geocode/search`, `render`,
  `import`, `results`, `layers`) and its modules (`geocode.py`,
  `overpass.py`, `osm_api.py`, `render.py`, `cache.py`, `schemas.py`).
- `/v1/convert` pipeline, rate limiting, Redis caching, ODbL attribution.

## Scope

### Phase 1 — Revert citymap on `/penplot` (DONE when)

- [x] `backend/penplot/templates/penplot.html`: delete fieldset "2. City
      map", `importCity` / `searchCity` / `cityBboxes` JS; renumber
      fieldsets 3–7 → 1–5.
- [x] `backend/penplot/ui.py`: drop `citymap_layers` context + `CITYMAP_*`
      imports.
- [x] Delete `test_penplot_page_pulls_in_citymap`; `/penplot` asserts NO
      `city_name` / `cityBtn` / `citymap/import` strings.

Acceptance criteria → tests (Phase 1):

| # | Criterion | Test |
|---|-----------|------|
| 1.1 | `/penplot` 200, no city section | `test_penplot_has_no_citymap_section` (negative markup asserts) |
| 1.2 | Upload → convert flow unchanged | existing `test_penplot_*` suite stays green |
| 1.3 | No dead JS references | page contains no `importCity`, `searchCity`, `cityBboxes` strings |

### Phase 2 — New `GET /citymap` page (DONE when)

- [x] New `backend/citymap/ui.py` + `backend/citymap/templates/citymap.html`,
      router registered in `backend/main.py` ahead of the catch-all proxy.
- [x] Shared Jinja includes (`_page_pen.html`, `_label.html`,
      `_display.html`, `_convert.js`) used by BOTH pages for sections
      Page & pen / Label / Display / stats / warnings / vpype / download.
- [x] Map section: search box → `geocode/search` → candidate `<select>`
      → MapLibre GL preview (pinned CDN version) on OpenFreeMap
      `planet/latest` tiles → layer checkboxes as style filters
      (`transportation.class` splits highways/roads/paths/rails/ferry;
      `water`, `waterway`, `building`, `aeroway` direct).
- [x] Load reads `map.getBounds()` → `POST /v1/citymap/import` with bbox +
      layers → title block auto-stamp → shared `convert()` flow.

Acceptance criteria → tests (Phase 2):

| # | Criterion | Test |
|---|-----------|------|
| 2.1 | `GET /citymap` 200, map container + search + layer checkboxes | `test_citymap_page_renders_map_section` (markup asserts) |
| 2.2 | Shared sections (Page & pen, Label, Display, stats, vpype, download) present on BOTH pages | `test_citymap_page_reuses_convert_sections` + existing `test_penplot_display` / `test_penplot_ui` |
| 2.3 | `/citymap` unknown path / bad query → 404/422, never 5xx | `test_citymap_page_negative` |
| 2.4 | Full user journey (search → pick → load → convert → download) verified manually against dev server (MapLibre CDN + tiles are client-side; E2E is manual per QA table) | manual checklist in PR/commit message |

Acceptance criteria → tests (Phase 3):

| # | Criterion | Test |
|---|-----------|------|
| 3.1 | New page markup complete | §2.1 test green |
| 3.2 | `/penplot` negative assertions | §1.1 test green |
| 3.3 | `geocode/search?limit=99` → 422 without network | `test_geocode_search_rejects_limit_over_10` (live-server fixture, validation only) |
| 3.4 | Full suite: `N passed`, 0 failed/errors | `.venv/bin/python -m pytest backend/tests/ -q` |

### Phase 3 — Tests, all hermetic (DONE when)

- [x] New page 200; contains map container, `geocode/search` hook, layer
      checkboxes, shared sections markup.
- [x] `/penplot` negative assertions (§Phase 1).
- [x] `geocode/search?limit=99` → 422 (no network).
- [x] Full suite green: `.venv/bin/python -m pytest backend/tests/ -q`.

## QA strategy (per affilio DoD test-type table, adapted)

| Work in this CR | Required test type |
|---|---|
| New `GET /citymap` page, `/penplot` template surgery | Page integration via live-server fixtures (real HTTP, never TestClient) — the repo default |
| MapLibre preview journey (CDN + tiles, client-side) | Manual E2E against dev server (no Playwright harness in this repo); markup-only server asserts |
| No complex algorithms | No unit tests required |

## NFR checklist (per affilio `NON_FUNCTIONAL_REQUIREMENTS.md` practice)

- [x] Performance: `/citymap` renders static template (no CPU work); tile
      traffic stays client ↔ OpenFreeMap, never through our backend.
- [x] Rate limiting: Load path reuses `/v1/citymap/import` (already
      rate-limited); page itself unthrottled like `/penplot`.
- [x] Security: bbox/layers validated by existing Pydantic schemas
      (`extra="forbid"`); MapLibre pinned to a fixed CDN version.
- [x] Usability: layer toggles instant (style filters); candidate picker
      for same-named places; title-block auto-stamp preserved.

## Schemathesis scope

No JSON API routes are added or changed (only the `GET /citymap` HTML
page) — scoped Schemathesis run NOT required. If implementation touches
shared middleware/auth, fall back to the full schema per affilio DoD.

## Definition of Done (adapted from affilio `docs/impl/DEFINITION_OF_DONE.md`)

- [x] Code: all listed files created/modified; env access only via
      `settings.*`; Pydantic v2 validators; new routes registered in
      `backend/main.py` ahead of the catch-all proxy.
- [x] TDD: failing test written before each phase's implementation
      (red → green → refactor); no new phase starts on red.
- [x] Test types: API/page integration via live-server fixtures (real
      HTTP, never TestClient) as default; unit tests only for
      algorithms/calculations; markup-only assertions for CDN-dependent
      pages; negative/boundary coverage included.
- [x] Full suite passes (`N passed`, 0 failed/errors).
- [x] Docs: this file's phases marked DONE as completed; `docs/index.md`
      status + last-updated synced (see `track-work` skill).
- [x] Serena memory updated for changed areas.
- [x] Git: commit per phase; push only when suite is green; no generated
      artifacts committed.
