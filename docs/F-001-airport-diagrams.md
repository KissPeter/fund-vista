# F-001 — Airport Ground-Diagram Blueprint Generator (`/airports`)

- Status: done
- Created: 2026-09-13
- Last updated: 2026-09-13
- Type: feature
- Scope: new independent `backend/airports/` module + `GET /airports` page + `/v1/airports/*` JSON API. No changes to `/v1/citymap/*`, `/v1/convert`, or `/penplot` behavior.

## Motivation

PenPlot product line "Airport diagrams" (Drawscape-style blueprint prints, Hungary) needs a generator that does **not** copy official AIP/FAA charts. All geometry comes from open data: OurAirports (CC0) for authoritative runway numbers + frequencies, OSM/Overpass (ODbL) for ground polygons (taxiways, aprons, terminals, hangars).

## Explicitly unchanged

- `backend/citymap/*`, `backend/penplot/*` logic untouched (only shared patterns reused: `require_rate_limit`, `{"error":{"code","message"}}` envelope, Redis-first cache, live-server test fixtures).
- No magnetic-declination model (true-north arrow only, TODO flagged). No live interactive preview editor. No vpype step in backend (post-generation, Inkscape/iDraw as before).

## Data sources & licensing

1. **OurAirports** (CC0, commercial OK, no attribution required) — fetched at runtime from `raw.githubusercontent.com/davidmegginson/ourairports-data/main/{airports,runways,airport-frequencies}.csv`, cached in Redis/memory for `cache_ttl_hours`. Fields used: `ident/name/latitude_deg/longitude_deg/elevation_ft/iso_country/municipality`; runway `le_/he_ident/lat/lon/heading/length_ft/width_ft/displaced_threshold_ft/surface`; frequency `type/description/frequency_mhz`.
   - **Fallback:** `le_/he_lat/lon` often empty on small fields → derive endpoints from airport center ± half `length_ft` along ident heading (`"13L" → 130°`). Surface `"CLOSED"` rows skipped.
2. **OSM via Overpass** (ODbL; printed piece = "produced work", attribution only): `way["aeroway"](around:3000,lat,lon)` + `relation["aeroway"](around:3000,lat,lon)`, `out geom`. Classified by `aeroway` tag: `runway|taxiway|apron|terminal|hangar`. OSM `runway` shapes kept; headings/idents/lengths always from OurAirports.

Credit line (footer + response `attribution`): `Runway/frequency data: OurAirports.com (CC0). Ground layout: © OpenStreetMap contributors (ODbL).`

## Pipeline (order matters)

1. Resolve ICAO → center lat/lon, name, country (`airports.csv`; accept `LHBP`, case/whitespace tolerant; reject unknown → 404 `airport_not_found`).
2. Pull matching `runways.csv` + `airport-frequencies.csv` rows.
3. Overpass `around` query at center (requested radius, auto-expanded to
   farthest runway threshold + 1000 m, capped at 8000 — the ARP can sit
   kilometers from the far end; LHBP's 13R is ~3.7 km out, so a fixed 3000
   silently dropped its taxiways); OSM Main API `/map` chunked fallback on total Overpass outage (same pattern as citymap).
4. Project to local flat plane (equirectangular meters about airport center — same math as `citymap/render.py:project`, good to ~1% at airport scale).
5. **Rotate once, apply to everything:** `rotation = target_angle − primary_runway_heading` (primary = longest open runway; target = vertical, i.e. runway along page Y). One matrix × all geometry (runways, taxiways, aprons, buildings, labels). Compass arrow drawn at `true_north_bearing − rotation` — never left pointing up.
6. Stroke treatments (single-ink plottable; "color" = weight, not ink):
   - Runways: geometry-bold — solid band of nine longitudinal paths across
     2× `width_ft` (deliberate schematic exaggeration: a true 45 m strip is
     ~1.6 mm wide at poster scale and unreadable; the ~0.3 mm infill pitch
     merges into a near-solid black bar with a normal pen). NOT a fat
     `stroke-width`: `parse_svg_vectors` discards stroke widths, so width
     must be geometry to survive import→convert, vpype and plot.
     Heavy `stroke-width` kept on the group for raw-SVG preview only.
   - Taxiways: thin centerlines. Aprons/terminals/hangars: thin closed outlines, `fill="none"`.
7. No title / frequency strip / footer in the artwork (client decision):
   the SVG is pure diagram geometry — name, frequencies and credits live
   on the page, in API responses and the convert title block, never plotted.
8. Heading badges: ident label past each threshold (the ident already
   encodes the heading — no degree ovals); displaced-threshold ticks where `> 0`.
9. Compass/declination arrow at rotation-adjusted angle (true north; WMM magnetic = TODO).
10. Single SVG, strokes only, `fill="none"`, A4 portrait viewBox with iDraw 2.0 margin.
12. Labels: all `<text>` carries `data-stroke-font="hershey"` — import lays
    them in crisp Hershey single-stroke (title-block faces; `°`/`•`
    synthesized, `©`→`(C)`), never raster-traced outlines. Vector input
    bypasses the section-2 Method controls by construction. SVG cache key
    carries a source-content hash — every renderer change busts it
    automatically, no manual version to forget.

## API (independent, mirrors citymap conventions)

- `GET /v1/airports/lookup?icao=LHBP` → airport meta + runway/frequency summaries (no OSM fetch; cheap, **unthrottled**).
- `POST /v1/airports/render {icao, width?, radius_m?, min_path_len_m?}` → full SVG render, `svg_url`, `path_counts`, `attribution`, `warnings`, `cache_hit` (**rate-limited** via shared `require_rate_limit`).
- `POST /v1/airports/import {icao, ...}` → render + register as penplot image, returns `image_id` for `POST /v1/convert` (vector branch). 422 when zero drawable features.
- `GET /v1/airports/results/{token}` → cached SVG (`image/svg+xml`).
- `GET /airports` → server-rendered page (ICAO search → lookup → render/import → shared convert partials). Static markup, unthrottled; expensive calls already limited.
- Validation: Pydantic v2, `extra="forbid"`; ICAO `^[A-Z0-9]{3,4}$` after upper/strip. Errors: `{"error":{"code","message"}}` (`airport_not_found` 404, `overpass_unavailable` 502, `ourairports_unavailable` 502, `invalid_params` 422).

## Module layout (`backend/airports/`)

- `config.py` (`AIRPORTS_` env: ourairports URLs/TTL, overpass mirrors reuse citymap list, `around_radius_m=3000`, `cache_ttl_hours=24`, `max_cached_bytes`).
- `ourairports.py` — CSV fetch (httpx, mirror: raw.githubusercontent only; Redis/memory cache) + `resolve_airport/runways/frequencies` + `runway_endpoints()` fallback + `heading_from_ident()`.
- `overpass.py` — `build_airport_query(lat,lon,radius_m)` + `fetch_airport_polygons()` (mirror failover, reuse citymap retry/backoff shape) + `split_aeroway()` → `{runway,taxiway,apron,terminal,hangar}` lon/lat rings/lines (own code, `out geom` node-coord form, not citymap bbox form).
- `geometry.py` — `project()` (equirect), `primary_runway()`, `rotation_for()`, `rotate_all()` single matrix; pure functions, unit-tested.
- `render.py` — blueprint SVG: freq strip, bands/outlines, badges, threshold ticks, north arrow at adjusted angle, footer; chrome = content here (unlike citymap chrome-free); `fill="none"` everywhere.
- `cache.py` — own prefix `fund-vista:airports:v1`, same Redis-first/memory-fallback contract as citymap.
- `schemas.py`, `router.py`, `ui.py` (+ `templates/airports.html` reusing penplot shared convert partials).

## QA / DoD

- Unit (no network): ident-heading parse, endpoint fallback math, rotation matrix (runway→vertical, north arrow = −rotation), SVG contains band/outlines/badges/arrow/footer strings.
- API integration via live-server fixtures (real HTTP, never TestClient): lookup 404/422 without network; render/import validation-only paths; `GET /airports` 200 markup + negative.
- Full suite: `.venv/bin/python -m pytest backend/tests/ -q` green. Manual E2E (LHBP + EGLL + small GA field) vs. real map before selling prints (OSM coverage varies — documented non-goal).
