# CR-001 — Split citymap out of `/penplot` into `/citymap`

- Status: todo
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

- [ ] `backend/penplot/templates/penplot.html`: delete fieldset "2. City
      map", `importCity` / `searchCity` / `cityBboxes` JS; renumber
      fieldsets 3–7 → 1–5.
- [ ] `backend/penplot/ui.py`: drop `citymap_layers` context + `CITYMAP_*`
      imports.
- [ ] Delete `test_penplot_page_pulls_in_citymap`; `/penplot` asserts NO
      `city_name` / `cityBtn` / `citymap/import` strings.

### Phase 2 — New `GET /citymap` page (DONE when)

- [ ] New `backend/citymap/ui.py` + `backend/citymap/templates/citymap.html`,
      router registered in `backend/main.py` ahead of the catch-all proxy.
- [ ] Shared Jinja includes (`_page_pen.html`, `_label.html`,
      `_display.html`, `_convert.js`) used by BOTH pages for sections
      Page & pen / Label / Display / stats / warnings / vpype / download.
- [ ] Map section: search box → `geocode/search` → candidate `<select>`
      → MapLibre GL preview (pinned CDN version) on OpenFreeMap
      `planet/latest` tiles → layer checkboxes as style filters
      (`transportation.class` splits highways/roads/paths/rails/ferry;
      `water`, `waterway`, `building`, `aeroway` direct).
- [ ] Load reads `map.getBounds()` → `POST /v1/citymap/import` with bbox +
      layers → title block auto-stamp → shared `convert()` flow.

### Phase 3 — Tests, all hermetic (DONE when)

- [ ] New page 200; contains map container, `geocode/search` hook, layer
      checkboxes, shared sections markup.
- [ ] `/penplot` negative assertions (§Phase 1).
- [ ] `geocode/search?limit=99` → 422 (no network).
- [ ] Full suite green: `.venv/bin/python -m pytest backend/tests/ -q`.

## Definition of Done (adapted from affilio `docs/impl/DEFINITION_OF_DONE.md`)

- [ ] Code: all listed files created/modified; env access only via
      `settings.*`; Pydantic v2 validators; new routes registered in
      `backend/main.py` ahead of the catch-all proxy.
- [ ] TDD: failing test written before each phase's implementation
      (red → green → refactor); no new phase starts on red.
- [ ] Test types: API/page integration via live-server fixtures (real
      HTTP, never TestClient) as default; unit tests only for
      algorithms/calculations; markup-only assertions for CDN-dependent
      pages; negative/boundary coverage included.
- [ ] Full suite passes (`N passed`, 0 failed/errors).
- [ ] Docs: this file's phases marked DONE as completed; `docs/index.md`
      status + last-updated synced (see `track-work` skill).
- [ ] Serena memory updated for changed areas.
- [ ] Git: commit per phase; push only when suite is green; no generated
      artifacts committed.
