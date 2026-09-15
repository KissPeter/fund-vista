# Airport-diagram page (`GET /airports`)

Date: 2026-09-15
Sources: `backend/airports/ui.py`, `backend/airports/templates/airports.html`,
`backend/penplot/templates/partials/*`, `backend/airports/router.py`,
`backend/airports/schemas.py`, `backend/airports/ourairports.py`,
`backend/airports/overpass.py`, `backend/airports/geometry.py`,
`backend/airports/render.py`, `backend/airports/cache.py`,
`backend/main.py`

 ICAO search → metadata lookup → runway-aligned blueprint render → import
into the shared convert pipeline. Same shared-partials architecture as
citymap: only fieldset "1. Airport (ICAO)" + its JS are page-local.

## Route wiring

- `GET /airports` (`backend/airports/ui.py::airports_ui`) renders
  `airports.html` via `ChoiceLoader` (local + penplot partials). Injects
  convert defaults + `airfield_layers` (7, pre-checked) and
  `context_layers` (7, unchecked) with human labels from `_LAYER_LABELS`.
- Registered last in `backend/main.py` (still before catch-all proxy).
  Page static/unthrottled; `POST /v1/airports/render|import|results` share
  the penplot per-IP bucket, `GET search|lookup` are cheap/unthrottled.

## UI controls (what the user sees)

### 1. Airport — ICAO (page-local fieldset)

| Control | Element / API | Function |
|---|---|---|
| Title line | `#apt_title` | Fixed top label `Name (ICAO) — place, country`, refreshed on lookup/render. The SVG itself carries no title. |
| Find text | `#apt_search` (60 chars, `Budapest, BUD, EGLL…`) | Freeform query. Typing hides the candidate picker. |
| Find airports | `#aptSearchBtn` → `searchAirports()` | `GET /v1/airports/search?q=&limit=5` (OurAirports). Fills `#apt_candidate`, auto-fills `#icao_code` with first hit. |
| Match picker | `#apt_candidate` (hidden until search) | Same-named/IATA disambiguation (`Name (ICAO/IATA) — municipality`). `change` → fill ICAO + immediate `renderAirport()`. |
| ICAO code | `#icao_code` (4 chars, uppercase, default `LHBP`) | Canonical key for lookup/render/import. `input` → debounced re-render (1200 ms, waits for full code). |
| Look up | `#icaoLookupBtn` → `lookupAirport()` | `GET /v1/airports/lookup?icao=`: fills `#airport_meta` (`name — municipality, country \| runways: 13L/31R, … \| freqs`) + title, no geometry fetch. |
| `#airport_meta` | `<div class=meta>` | Runway ident pairs + first 4 frequencies. |
| Detail radius | `#apt_radius` 500–6000 step 250 = 3000 m | `radius_m`: Overpass `around` radius from ARP. Backend may **expand** it to farthest runway end + 1000 m margin (cap 8000 m, warning `radius_expanded_to_cover_runways`). |
| Zoom (fill page) | `#apt_zoom` 0.25–5 step 0.25 = 1 | `zoom`: 1 = fit all content; >1 crops edges to fill page; <1 adds margin. Applied in `render_diagram` about scene center. |
| Min detail | `#apt_minlen` 0–50 step 1 = 5 m | `min_path_len_m`: drop projected OSM polylines shorter than N meters. |
| Layer checkboxes | `.apt_layer` — airfield checked, context unchecked | `layers[]` payload. Airfield: runways, taxiways, aprons, terminals, hangars, stands, stopways. Context (fires 2nd Overpass query): highways, roads, paths, rails, waterway, water, buildings. Omit = airfield default. |
| Taxiway refs | `#apt_twy_labels` unchecked | `taxiway_labels`: draw OSM `ref` designators (A1, B3…) at way midpoints. Annotation of the taxiway layer — hidden with it. |
| Render diagram | `#aptBtn` → `renderAirport()` | Full flow below. Debounced auto re-renders: sliders 900 ms, layer/label toggles 400 ms, ICAO typing 1200 ms — never hammers Overpass per keystroke. |
| `#meta` help | static text | Explains OurAirports CC0 + OSM ODbL sources, vector-input note, cache note, "spot-check small fields" caveat. |

### 2–6. Shared convert sections

Identical partials to penplot (legends renumbered "2. Method" … "6.
Display") + `#resetBtn` + shared `_result_panel.html`. The convert panel
is the **only** preview — there is no second SVG `<img>`; the render step
feeds straight into import+convert.

## JS functions (page-local + shared `_convert.html`)

- `icaoCode()` — trim+uppercase `#icao_code`.
- `setAirportTitle(body)` — `Name (ICAO) — municipality, country` into
  `#apt_title`.
- `searchAirports()` — ≥2 chars guard → fetch search → rebuild options,
  `aptCandidates = {index: icao}`, unhide picker, status hints.
- `lookupAirport()` — fetch lookup → `le_ident/he_ident` pairs +
  `type freq.toFixed(3)` (first 4) into `#airport_meta`; returns body.
- `renderAirport()` — the one flow, **two** POSTs back-to-back:
  1. `POST /v1/airports/render {icao,radius_m,min_path_len_m,width:1000,
     zoom,layers,taxiway_labels}` (422/429/502 mapped to status line);
  2. `POST /v1/airports/import` (same payload) → `imageId`,
     `currentFile=null`, `#meta` = `name (icao) — rotation X° (N paths)` +
     warnings. New ICAO (tracked in `lastPlottedIcao`) stamps the label
     block (enabled+border, `Name ICAO`, 5 mm right pad) without clobbering
     hand-edits on slider refreshes. Then `syncOutputs()` +
     `convert(false)`.
- Wiring: lookup/search/candidate/render buttons as above;
  `scheduleRender(delayMs)` debounce shared by all airport controls
  (sliders 900 ms, checkboxes 400 ms, ICAO 1200 ms).

## Backend (what the server does)

- `GET /v1/airports/search?q&limit` — `ourairports.search_airports` over
  cached OurAirports CSVs (name/municipality/ICAO/IATA ranked);
  `{query, candidates[{icao,iata,name,municipality,iso_country,lat,lon,type}],
  cache_hit}`. 502 `ourairports_unavailable`.
- `GET /v1/airports/lookup?icao` — `_lookup`: normalize (3–4 alnum,
  uppercase), `resolve_airport` + `airport_runways` + `airport_frequencies`
  (CSV cache hits → `ourairports_cache_hit`) → `{icao,name,municipality,
  iso_country,lat,lon,elev_ft,iata,runways[{le/he_ident,length/width_ft,
  surface,headings}],frequencies[{type,description,frequency_mhz}]}`. No OSM.
- `POST /v1/airports/render` (rate-limited) — lookup → effective radius
  (`_effective_radius_m`: project runway endpoints via
  `geometry.project`+`runway_endpoints`, expand to farthest+1000 m) →
  Redis-first Overpass `around` fetch (`_load_polygons`: airfield query
  `build_airport_query` + optional context query `build_context_query`,
  5 mirrors, context outage degrades to empty layer +
  `context_unavailable`) → SVG cache key (icao, radius, minlen, width,
  sorted layers, zoom, tlabels, renderer content-hash version) → hit:
  reuse SVG + recompute counts via `split_aeroway/split_context`;
  miss: `_build_svg` → `split_aeroway` (OSM→7 airfield classes) +
  `extract_taxiway_refs` + `split_context` → `render_diagram(...)` →
  cache → `{rotation_deg, path_counts, raw_counts,
  svg_url:/v1/airports/results/{sha1}, warnings, cache_hit}`.
- `POST /v1/airports/import` (rate-limited) — same lookup+fetch+build,
  422 when 0 paths and no runways, `imaging.parse_svg_vectors` validation,
  `penplot_store.put_image_bytes` → `{image_id, …}` for stock
  `POST /v1/convert` (vector branch).
- `GET /v1/airports/results/{token}` — serve cached diagram SVG.
- `render_diagram` (`render.py`): true runway geometry from OurAirports
  endpoints (length/width/surface + heading fallback from ident), rotate
  scene so primary runway is horizontal (`geometry.rotation_for_heading`),
  exaggerated runway width + infill fractions, OSM polygons for
  taxiway/apron/terminal/hangar/stands/stopways + context classes, taxiway
  ref annotations, short-path filter, zoom-about-center, footer credit in
  the artwork (`Runway/frequency: OurAirports CC0. Ground: © OSM ODbL`).
- Upstreams/cache (`AIRPORTS_*` env): OurAirports GitHub-raw CSVs (20 s),
  Overpass 5 mirrors (30 s, 1 retry), 24 h TTL, 8 MB Redis cap.
- Errors: `{"error":{"code","message"}}`; codes `airport_not_found`,
  `ourairports_unavailable`, `overpass_unavailable`, `result_not_found`,
  plus shared `invalid_params/image_not_found/rate_limited`.
