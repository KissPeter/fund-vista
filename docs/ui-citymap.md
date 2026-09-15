# City-map page (`GET /citymap`)

Date: 2026-09-15
Sources: `backend/citymap/ui.py`, `backend/citymap/templates/citymap.html`,
`backend/penplot/templates/partials/*`, `backend/citymap/router.py`,
`backend/citymap/schemas.py`, `backend/citymap/layers.py`,
`backend/citymap/geocode.py`, `backend/citymap/overpass.py`,
`backend/citymap/osm_api.py`, `backend/citymap/render.py`,
`backend/main.py`

 Place → pannable vector preview → load-visible-bbox → shared convert
pipeline. Convert sections (Method, Cleanup, Page & pen, Label, Display,
result panel, `_convert.html` JS) are the single-source partials owned by
`/penplot`; this page adds only the Place fieldset + MapLibre preview.

## Route wiring

- `GET /citymap` (`backend/citymap/ui.py::citymap_ui`) renders
  `citymap.html` via a `ChoiceLoader` (local dir first, then
  `backend/penplot/templates` for shared partials). Injects the same
  convert defaults as penplot plus `citymap_layers` (id+label in
  `LAYER_ORDER`) and `maplibre_version = "4.7.1"`.
- Registered in `backend/main.py` after penplot, before the catch-all
  proxy. Page static/unthrottled; `POST /v1/citymap/render|import` share
  the penplot per-IP rate-limit bucket.

## UI controls (what the user sees)

### 1. Place (page-local fieldset)

| Control | Element / API | Function |
|---|---|---|
| City text | `<input id=city_name maxlength=120 value="Budapest">` | Freeform place query. Typing clears the candidate picker (stale-pick guard). |
| Find places | `<button id=citySearchBtn>` → `searchCity()` | `GET /v1/citymap/geocode/search?city=&limit=5`. Lists Nominatim candidates in `#city_candidate`, flies preview to first bbox. Status hints "Pick a match … then Load". |
| Match picker | `<select id=city_candidate hidden>` | Same-named-place disambiguation. `change` → `fitCandidate(bbox)` (no fetch). Index→bbox map held in `cityBboxes`. |
| Map preview | `<div id=map>` (MapLibre GL 4.7.1, CDN css+js) | Vector preview on free OpenFreeMap tiles (`tiles.openfreemap.org/planet/latest/{z}/{x}/{y}.pbf`, maxzoom 14, no key). Center `[19.04,47.5]` zoom 11. NavigationControl top-right; drag/scroll work. **What-you-see-is-what-you-load**: import uses `map.getBounds()`. |
| Layer checkboxes | `<input class=city_layer value=<id>>`, highways+roads pre-checked | Dual purpose: (a) live preview toggle via `syncPreviewLayers()` → `setLayoutProperty(visibility)`, (b) `layers[]` payload for import. Water/waterway/building/aeroway preview layers are always defined but hidden unless ticked. |
| Min detail | `<input range id=city_minlen 0–50 step=1 value=10>` + output | `min_path_len_m`: drop projected polylines shorter than N meters (pen-plotter fast path). |
| Load city map | `<button id=cityBtn>` → `importCity()` | `POST /v1/citymap/import {bbox, layers, min_path_len_m, width:1000}` → sets `imageId`, stamps label block (enabled+border, place name, 5 mm right pad), `syncOutputs()`, `convert(false)`. First load can take a minute; repeats served from cache. |
| `#meta` help line | static text | Explains visible-area semantics, vector-input note (shading sliders don't apply; pen/page/label/display do), ODbL credit. |

Layer ids (labels from `layers.py`, draw/first-match order in
`LAYER_ORDER`): `highways` (motorway/trunk/primary), `roads`
(secondary→track), `paths` (foot/cycle/steps), `rails` (rail+route
relations), `aeroway`, `waterway` (flowing), `water` (standing, outlines),
`buildings` (outlines), `ferry` (route relations). A way matching several
layers is drawn once (first match in `LAYER_ORDER` wins).

### 2–6. Shared convert sections

Identical to the penplot page (see `docs/ui-penplot-image.md`):
Method (`legend "2. Method"`) → Cleanup ("3.") → Page & pen ("4.") →
Label ("5.") → Display ("6.") → `#resetBtn`, then shared
`_result_panel.html` (status/preview/stats/warnings/vpype/download).

## JS functions (page-local scripts + shared `_convert.html`)

Preview script (first `<script>`):

- `TILE_URL`, `LINE()` helper, `map = new maplibregl.Map({style…})` —
  style layers: `bg`, `lyr-water`, `lyr-waterway`, `lyr-building`,
  `lyr-aeroway`, `lyr-ferry` (dashed), `lyr-rails`, `lyr-highways`,
  `lyr-roads`, `lyr-paths` (dashed). Zoom carries pre-generalized detail.
- `STYLE_IDS` — city layer id → preview style ids.
- `syncPreviewLayers()` — checkbox state → style visibility; called on
  `map load` + every `.city_layer change`.
- `map.addControl(new maplibregl.NavigationControl(), "top-right")`.

Data script (second `<script>`, includes shared `_convert.html`):

- `fitCandidate(bbox)` — `map.fitBounds([[w,s],[e,n]], {animate:false})`.
- `searchCity()` — fetch geocode/search, rebuild options as
  `display_name`, unhide picker, fit first candidate. Errors via
  `setStatus(…, "error")`; busy guard via `setBusy/drain` (`depth`
  counter shared with convert).
- `importCity()` — gather ticked `.city_layer`, read `map.getBounds()` →
  `{south,west,north,east}`, POST import, 429-aware error mapping, set
  `imageId`, `currentFile=null` (imports have no local file; on cache
  expiry just re-import), write `#meta` (`place — layers (N paths)` +
  warnings), auto-stamp label block, `convert(false)`.
- Wiring: `citySearchBtn click`, `city_candidate change`,
  `city_name input` (hide picker), `cityBtn click`. No auto re-import on
  pan/zoom or layer toggle — preview-only until Load is pressed.

## Backend (what the server does)

- `GET /v1/citymap/layers` — selectable layers in draw order
  (`{id,label,description}`).
- `GET /v1/citymap/geocode/search?city&limit` — Nominatim
  (`geocode.search_places`), up to 10 candidates
  `{display_name,bbox,lat,lon,category,type}` + `cache_hit`. 404
  `city_not_found` / 502 `nominatim_unavailable`.
- `GET /v1/citymap/geocode?city` — single best bbox
  (`geocode.geocode_city`, Redis-cached 24 h).
- `POST /v1/citymap/render` (rate-limited) — `_resolve_area`
  (city→geocode or explicit bbox + `check_bbox_span` ≤ 0.8°/side) →
  Redis-first raw fetch (`_load_raw`: Overpass QL built by
  `build_overpass_query` from `selectors_for(layers)` across 5 mirrors
  with failover; on Overpass failure falls back to chunked OSM Main API
  `/api/0.6/map` via `osm_api.fetch_osm_api`, warning `osm_api_fallback`)
  → `split_elements` (tags→layer geoms, first-match-wins) →
  `render_svg(geoms,bbox,width,min_path_len_m)` (equirectangular project,
  short-path filter, one `<g id="citymap-<layer>">` per layer, chrome-free
  so the plotter draws only streets) → cache SVG 24 h → `{svg_url:
  /v1/citymap/results/{sha1}, path_counts, raw_counts{nodes,ways,relations},
  attribution, warnings, cache_hit}`. SVG-hit-without-raw path counts
  straight from the cached document (`counts_from_cached_svg`).
- `POST /v1/citymap/import` (rate-limited) — same resolve+fetch+split+
  render, then `imaging.parse_svg_vectors` validation +
  `penplot_store.put_image_bytes` → `{image_id, …}`. Empty result (0
  paths) → 422 with "tick more layers or bigger area" hint. The returned
  `image_id` converts via stock `POST /v1/convert` on the **vector
  branch** (shading sliders ignored; pen/page/label/display apply).
- `GET /v1/citymap/results/{token}` — serve cached SVG (sha1 token,
  404 `result_not_found` on expiry).
- Config (`CITYMAP_*` env, `citymap/config.py`): 5 Overpass mirrors,
  30 s timeouts, OSM API chunking (0.1°→0.025° adaptive), Nominatim URL,
  24 h TTL, 8 MB Redis value cap.
- Errors: same `{"error":{"code","message"}}` envelope; Pydantic
  `extra="forbid"` + `city XOR bbox` validator.
