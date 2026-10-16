# Data-provider terms & backend compliance (issue #59)

Date: 2026-10-15
Scope: fund-vista backend part of the third-party-licence cleanup
(`docs/third-party-licenses-plan.md`, branch `feat/third-party-licenses-plan`).
Shop-side consumer: pen-pixel-shop `docs/terms-requirements.md`
(pen-pixel-shop#950, #947) — the **condition + deviation** facts below are
the rows that document feeds.

Fixed decisions (not re-negotiated here):
D1 Potrace (GPL-2.0-or-later) and vpype-flow-imager (GPL-3.0) are
server-side-only; output SVG/G-code is not GPL. D2 they never reach the
browser and are never installed client-side. D3 generated images
(SVG/plot) carry **no footer/credit/attribution**; attribution lives in the
ToS, the UI and API metadata. D4 new dependencies only per the shop-repo
`docs/third-party-policy.md` allowlist (this change adds none).

## 1. Nominatim (geocoding — `backend/citymap/geocode.py`)

Upstream terms (operations.osmfoundation.org/policies/nominatim):

- Max **1 request/s** against the public instance.
- A real `User-Agent` identifying the application; a `Referer` or contact
  URL is asked for on heavier use. Stock/generic client UAs get blocked.
- Cache results on our side; no bulk geocoding on the public instance.
- Commercial use of the public instance is allowed within the same limits;
  sustained/heavy use must move to a self-hosted instance.
- Results are OSM data → ODbL attribution applies (see §5).

Backend compliance:

- Cache-first: `geocode` / `geocode-search` Redis keys, TTL
  `cache_ttl_hours` (24 h). Hits never touch the upstream.
- Process-wide throttle `_throttle_nominatim()` on every cache miss:
  `nominatim_min_interval_s = 1.0` (new knob, `CITYMAP_` prefix), enforced
  under an `asyncio.Lock` so concurrent misses serialize into compliant
  spacing. Unit-tested via state manipulation, never `sleep()`.
- Identification: `User-Agent: fund-vista-citymap/1.0
  (https://github.com/KissPeter/fund-vista; pen-plot city maps)` plus
  `Referer: https://github.com/KissPeter/fund-vista`
  (`nominatim_referer` knob).
- Query shape stays small: `limit` clamped to 1–10, single `q` per user
  action, 15 s timeout.

Deviations found & fixed in this issue:

| # | Deviation | Fix |
|---|-----------|-----|
| N1 | No throttle — concurrent misses could exceed 1 req/s | `_throttle_nominatim()` on both `geocode_city` and `search_places` |
| N2 | `Referer` pointed at the wrong repo (`anomalyco/opencode`) | Now `settings.nominatim_referer` (fund-vista URL) |
| N3 | `User-Agent` had no contact URL | Contact URL added to both citymap and airports UAs |

## 2. Overpass public instances (`backend/citymap/`, `backend/airports/`)

Upstream terms (OSM wiki “Overpass API / public instances” + instance pages):

- Shared public resource: keep queries small, use a short `timeout:`,
  cache aggressively, identify the app with a real `User-Agent`.
- Do not hammer instances in parallel; back off and fail over instead of
  retry-storming a struggling mirror.
- Data is OSM → ODbL attribution applies (see §5).

Configured mirrors, tried **sequentially** (first healthy wins):

1. `https://overpass-api.de/api/interpreter`
2. `https://overpass.kumi.systems/api/interpreter`
3. `https://overpass.private.coffee/api/interpreter`
4. `https://overpass.openstreetmap.fr/api/interpreter` (OSM-FR, EU)
5. `https://overpass.nchc.org.tw/api/interpreter` (NCHC, Taiwan — non-EU
   operator for full-EU outages)

Backend compliance:

- One unioned query per render (`union_members` + `wrap_query`,
  `[timeout:25]`); airport context layers fire exactly one second query and
  only when selected.
- Per-mirror retries on retryable failures only (HTTP 408/429/5xx +
  network errors, `overpass_retries = 1`, exponential backoff + jitter);
  400/403/404 fail fast to the next mirror, never retried.
- Short HTTP timeout (`overpass_timeout_s = 30.0`) so a hung mirror fails
  over to the OSM Main API fallback (citymap) instead of holding a worker.
- `User-Agent` identifies the app on every POST (Overpass answers 406 to
  generic `python-httpx/*` UAs).
- Raw payloads are cached: citymap per-(tile, layer) 168 h
  (`tile_cache_ttl_hours`), airports per-(lat, lon, radius) 24 h; radius
  bucketing (`radius_bucket_m = 500`) stops slider ticks fragmenting the
  cache.
- Concurrency cap (new): `overpass_max_concurrent = 2` semaphore in both
  `citymap/overpass.py` and `airports/overpass.py` — concurrent renders
  queue locally instead of fanning out parallel POSTs per mirror.

Deviations found & fixed in this issue:

| # | Deviation | Fix |
|---|-----------|-----|
| O1 | No cap on parallel upstream POSTs (N concurrent renders = N× parallel mirror hits) | `_overpass_semaphore()` around every POST in both modules |
| O2 | UA had no contact URL | Contact URL added (see §1) |

## 3. OSM Main API v0.6 `/map` (citymap fallback — `backend/citymap/osm_api.py`)

Upstream terms (api.openstreetmap.org + usage policy):

- `GET /api/0.6/map?bbox=` caps the bbox at **0.25 deg²** and practically
  at ~50k nodes; the editing API is for small reads, not bulk downloads.
- Keep requests small and cached; identify the app.

Backend compliance:

- Bbox guard `max_bbox_deg = 0.8` at the router; cells start at
  `osm_api_max_deg = 0.1` and subdivide adaptively to `osm_api_min_deg =
  0.025` on HTTP 400 (dense city centers), so no request ever approaches
  the 0.25 deg² limit.
- Retries only on transient statuses (408/429/5xx + network, `retries =
  2`); 400 subdivides instead of retrying.
- `User-Agent` on every cell GET; results merge into the same tile cache
  as Overpass payloads, so the fallback is cached identically.
- `defusedxml` parsing (already a dependency — no new dep).

No deviation found.

## 4. OurAirports (airport metadata — `backend/airports/ourairports.py`)

Upstream terms:

- `davidmegginson/ourairports-data` CSVs are **CC0 / public domain**: no
  permission needed, no attribution legally required, commercial use free.
- Source is GitHub raw (`ourairports_base_url`), so GitHub + politeness
  rules apply: cache, identify, handle failures gracefully.

Backend compliance:

- Redis-first (`ourairports:<name>` keys, `cache_ttl_hours` 24 h) plus an
  in-process digest cache so the ~12 MB `airports.csv` is parsed once per
  TTL per worker; search rows are additionally mirrored cluster-wide.
- `User-Agent` on every CSV GET; non-200 and unexpected payloads raise
  `OurAirportsError` → 502 envelope (no silent bad data).
- Attribution given **voluntarily** (CC0 does not require it): API
  `attribution` field + `/airports` page copy credit OurAirports.com.
  Never plotted into the SVG (D3).

No deviation found.

## 5. Attribution placement (D3 — all OSM-derived output)

- OSM data is **ODbL 1.0**: public use requires “© OpenStreetMap
  contributors” credit with a link to `https://osm.org/copyright`.
- Citymap: `ATTRIBUTION = "© OpenStreetMap contributors · ODbL 1.0 ·
  https://osm.org/copyright"` on `RenderResponse`/`ImportResponse`; the
  renderer (`render.py`) is chrome-free by construction (no `<text>`, no
  comments) and `chrome.py` strips city-roads import chrome the same way.
- Airports: `ATTRIBUTION = "Runway/frequency data: OurAirports.com (CC0).
  Ground layout: © OpenStreetMap contributors (ODbL)."` on
  lookup/render/import responses; `render.py` draws pure diagram geometry
  (no title/strip/footer/compass — covered by `test_airports.py`).
- UI: `/citymap` credits OSM/ODbL + OpenFreeMap/OpenMapTiles under the
  preview; `/airports` credits OurAirports (CC0) + OSM (ODbL) in the page
  copy. Nothing is plotted.
- Preview tiles (`tiles.openfreemap.org`, MapLibre): free, no key, no
  request limits per provider; attribution shown under the preview
  (`© OpenMapTiles © OpenStreetMap contributors`).

## 6. What changed in this issue (code)

- `backend/citymap/config.py`: `nominatim_min_interval_s` (1.0),
  `nominatim_referer`, `overpass_max_concurrent` (2); UA gains contact URL.
- `backend/airports/config.py`: `overpass_max_concurrent` (2); UA gains
  contact URL.
- `backend/citymap/geocode.py`: process-wide 1 req/s throttle on cache
  misses (both entry points); `Referer` from settings.
- `backend/citymap/overpass.py`, `backend/airports/overpass.py`:
  semaphore-guarded POSTs (max 2 concurrent per worker).
- `backend/tests/test_data_provider_terms.py`: hermetic ToS-compliance
  suite (config defaults, headers, throttle, cache-saves-upstream,
  fallback order + fail-fast, attribution + chrome-free artwork).
- No new dependencies; no licence change (all touched code is existing
  permissive-licensed app code; no GPL/AGPL/LGPL added).
