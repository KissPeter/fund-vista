# Citymap module (backend/citymap) — OSM city maps, city-roads style

Purpose: load a city map (e.g. Budapest) from OpenStreetMap, render one SVG
group per selectable layer, cache everything in Redis. Chrome-free SVG
(no location caption, no credit comment) for the pen plotter.

Module map (`backend/citymap/`):
- `layers.py` — 9 selectable layers in `LAYER_ORDER` (first-match wins):
  highways, roads, paths, rails, aeroway (airports), waterway
  (rivers/streams), water, buildings, ferry. Tag selectors mirror the
  Drawscape/Mapbox-Streets-v8 set + paths.
- `overpass.py` — bbox QL builder, mirror-fallback fetch
  (`overpass-api.de`, `overpass.kumi.systems`), `split_elements` (relation
  members claimed first, then first-match way assignment — no double-ink).
- `geocode.py` — Nominatim place resolution (proper UA header, 1 req/s
  friendly via cache). `check_bbox_span` guard (`max_bbox_deg` 0.8).
- `render.py` — equirectangular meters projection, `<g id="citymap-<layer>">`
  groups, closed outlines for buildings/water/aeroway, `min_path_len_m`
  filter (city-roads minLength equivalent).
- `chrome.py` — `strip_city_roads_chrome()`: removes generator/credit
  comments + trailing `<text>` captions from upstream city-roads exports
  (ElementTree pass + regex fallback, idempotent).
- `cache.py` — Redis (`fund-vista:citymap:v1:{kind}:{sha1}`, setex TTL)
  with in-process fallback; oversized values skipped. Wired via
  `configure_citymap_redis()` in `backend/main.py` startup.
- `schemas.py` / `router.py` — `/v1/citymap/layers`, `/geocode?city=`,
  `POST /render` (exactly one of city/bbox, `extra="forbid"`),
  `POST /import` (same body; renders + registers as penplot SVG image,
  returns `image_id` for `POST /v1/convert` — vector branch),
  `/results/{sha1}` served from Redis. Shares penplot rate-limit bucket.
  `attribution` field carries the ODbL credit (response metadata, not artwork).
- `/penplot` page pulls city maps in: "2. City map" fieldset (city input,
  layer checkboxes, min-detail slider, Load button → `/v1/citymap/import`
  → auto-convert with current pen/page/display settings).
- Ops lessons (verified live 2026-09-13): every Overpass union member MUST
  end with `;` or the query 400/406s (builder appends it); Overpass
  answers 406 to generic HTTP-client UAs, so every POST carries the app
  `User-Agent`. `overpass.kumi.systems` timed out from here while
  `overpass-api.de` answered in ~2-4s — mirror order matters.

Upstream reference: https://github.com/anvaka/city-roads
(API.md console recipes, Query.js filters, `src/lib/svgExport.js` chrome).
OSM data is ODbL 1.0 — public use requires attribution; stripping it from
the SVG does not waive that (kept in API metadata).

Frontend: `src/services/citymapApi.ts` — `loadCityMap({city|bbox, layers})`,
`geocodeCity()`, `getCitymapLayers()`, `CITYMAP_LAYER_LABELS`.

Tests: `backend/tests/test_citymap.py` — all hermetic (no OSM network);
only `/layers` + validation 422s go over HTTP.
Env: `CITYMAP_CACHE_TTL_HOURS`, `CITYMAP_MAX_BBOX_DEG`,
`CITYMAP_OVERPASS_TIMEOUT_S` (see backend/.env.example).
