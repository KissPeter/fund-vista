# F-008 Airfield hybrid source (Overture + Overpass)

Status: implemented in the working tree for airport designer and airport
traffic; not committed. The key words MUST, MUST NOT, SHOULD and MAY are to be
read as in RFC2119.

## Why

Public Overpass is slow and often at capacity (LHBP 3 km: 26-39 s per query,
504s across mirrors). Overture Maps `base/infrastructure` answers the airfield
core in about 7 s with no rate limit. Every Overture airport feature carries an
OpenStreetMap record id (`w567480715@7`), so it is a monthly copy of the same
OSM ways, not independent data.

Measured on LHBP, 3 km circle, Overpass as reference, matched by OSM way id:
runway, taxiway, apron, stopway and taxiway labels agree 100% (precision and
recall). Overture lacks stands (`parking_position`), hangars and terminals.

## Design

One module, `backend/airports/sources.py`, MUST be the only place that decides
where airfield geometry comes from. Callers keep today's result shape
(`split_aeroway` classes plus taxiway refs).

| Layer | Primary | Fallback |
|---|---|---|
| runway, taxiway (incl. taxilane), apron, stopways, taxiway labels | Overture | full Overpass airfield query (today's path) |
| terminal, hangar, stands | Overpass, reduced query (`aeroway` = parking_position, hangar, terminal) | none: layer is omitted with warning `osm_layers_unavailable` |

- Overture and the reduced query run concurrently. An Overture failure or an
  empty Overture result MUST fall back to the full Overpass query, so behaviour
  never gets worse than today. A reduced-query failure keeps the Overture
  layers.
- Failover cost: a request MAY send the reduced query and then the full query,
  so at most one extra Overpass query. The Overture read is bounded by
  `AIRPORTS_OVERTURE_TIMEOUT_S`. After a failed read, Overture is skipped for
  60 s (`OVERTURE_BACKOFF_S`), and during that window requests go straight to
  the full Overpass query, so an outage does not cost a timeout per request.
- Shared filters, applied to both sources by one function, also when
  `AIRPORTS_OVERTURE_ENABLED=false` (deliberate: the OSM-only output gets the
  same circle filter and runway floor):
  - `around` semantics: keep a feature when its nearest point is within the
    radius (circle, not bbox). A closed ring (polygon) that encloses the centre
    is kept even when no edge is within the radius.
  - drop `runway` ways shorter than 50 m (closed stubs such as LHBP `03/21`).
- Overture reader: DuckDB with `spatial` and `httpfs`. The extensions are
  loaded first and installed only when `LOAD` fails. The bbox is pushed down to
  the parquet `bbox` struct, then the circle filter runs. The SQL is built by
  `build_overture_sql`, which MUST reject any release that does not fully match
  `YYYY-MM-DD.N` before the query is built.
- Release: pinned in config (`AIRPORTS_OVERTURE_RELEASE`). Old releases are
  removed upstream. When the pinned release is not found (HTTP 404 or an empty
  S3 glob), the reader MUST retry once with the `latest` release from
  `https://stac.overturemaps.org/catalog.json`, cached in a module variable. A
  STAC failure falls through to the Overpass fallback. The release actually
  read is returned in `AirfieldResult.overture_release`.
- Labels come from `source_tags.ref` on `class=taxiway`; same designators as
  `extract_taxiway_refs`.
- Splitting and filtering of both paths run in a worker thread
  (`asyncio.to_thread`), so the event loop is not blocked by CPU work.
- Caching: Redis, per source and layer set, same TTL policy as Overpass
  payloads. The airfield key includes the Overture release (or `off`) and
  `SOURCES_VERSION`. The airport render keys include `overture` (enabled),
  `release` and `sources`. The traffic airfield L3 key includes the same three
  values, and the traffic renderer version is `r7`.
- Degraded results are never cached. A result whose warnings contain
  `overture_unavailable` or `osm_layers_unavailable` (`is_degraded`) is served
  once and MUST NOT be written to the airfield Redis cache, the airport render
  cache (both the request path and the job path), or the traffic basemap L3.
  An `overture_empty` fallback is complete Overpass data and is cached.
- Provenance: `AirfieldResult` carries `sources` per layer and the Overture
  release. The API response does not expose them yet (see Deferred).

## Impact

| Feature | Code | Change |
|---|---|---|
| Airport designer | `backend/airports/fetching.py` `_load_airfield` | Uses `sources.load_airfield`. Context layers (highways, water, buildings) stay on Overpass `build_context_query`. |
| Airport render and job paths | `backend/airports/router.py` `_build_and_store`, `backend/jobs/_exec_airports.py` | Skip the render store for a degraded result. |
| Airport traffic | `backend/traffic/basemap.py` `_fetch_layer("airfield")`, radius 6000 m | Uses `sources.load_airfield`; keeps runway/taxiway/apron/terminal. Namespace `osm2` moved to `osm3`. Other basemap layers (country, motorways, waterways, water) are unchanged and keep their keys. |
| Map designer (citymap) | `backend/citymap/layers.py` `aeroway` = `way["aeroway"]({bbox})` inside one unioned query | Not changed. See Deferred. |
| 3D city view | `pen-pixel-shop`, not this repo: `BlueprintPreview.tsx`, route `/en/custom-design/city-view` (internal name `blueprint`). Client-side MapLibre + deck.gl on OpenFreeMap vector tiles; no `/v1/blueprint/render`. The plot SVG goes through the existing upload path (`/v1/images`, `/v1/tokens`, `/v1/convert`). | None. It never calls Overpass, so this change does not touch it. Only OSM/OpenFreeMap attribution stays as is. |

`POST /v1/compose-sheet` MUST remain the only sheet composer and keep one code
path for preview, personalisation and order file (AGENTS.md). This change only
swaps the geometry input of the airport path.

`raw_counts` for airports: `airfield_raw_counts` reports the number of kept
polylines per class under `ways`, with `nodes` and `relations` set to 0. The
keys stay so existing consumers still parse. See `docs/ui-airports.md`.

## Tests

No test uses the network. The fakes are in `backend/tests/test_airfield_sources.py`
and `backend/tests/test_airfield_fetch.py`.

- Filters: a line whose endpoints are outside the circle but whose segment
  crosses it is kept; a feature in the bbox corner outside the circle is
  dropped; a polygon enclosing the centre is kept; the 50 m runway floor
  applies, and does not apply to other layers.
- Routing: the layer routing and `sources` map; taxiway labels come only from
  `taxiway` with a ref.
- Failover: Overture error or empty result uses full Overpass with
  `overture_unavailable` or `overture_empty`; reduced Overpass failure keeps
  Overture layers with `osm_layers_unavailable`; both failing raises; Overture
  disabled runs the full query only.
- Release: a pinned 404 retries once with the STAC latest; a STAC failure falls
  back to Overpass; an outage is skipped for the backoff window.
- Concurrency: Overture and the reduced query overlap (a sequential
  implementation fails this test).
- SQL: the bbox pushdown and subtype/class filter text; a release that does not
  fully match (quote injection, trailing newline, whitespace) is refused.
- Cache: degraded results are not cached in the airfield cache, the airport
  render path (request and job) or the traffic basemap L3; a fallback after an
  empty read is cached; the render keys and the airfield and L3 keys change with
  the Overture switch, the release and `SOURCES_VERSION`.

## Risks

- Overture lags OSM by up to a release cycle (monthly): a newly built or closed
  runway shows late. Acceptable for posters; MUST be stated in the meta.
- New dependency: the `duckdb` Python library, embedded in the backend process
  (no server, no daemon, nothing to run). Its `spatial` and `httpfs` extensions
  download on first use, so the container image SHOULD preinstall them. Check
  procurement before adding (#cop-procurement).
- S3 egress from the host and a cold start of about 7 s per airport.
- A degraded render is not stored, so its `svg_url` returns 404 until a later
  request renders it again. The preview of a degraded airport render can
  therefore fail. This is the chosen trade-off, see Deferred.

## Phases and tickets

1. Airport designer and airport traffic (shared fetcher). Implemented in the
   working tree.
2. Map designer `aeroway` layer. Not started.

The 3D city view needs no phase (see Impact).

## Deferred

- Golden LHBP fixture: recorded Overture rows and Overpass elements, asserting
  per-layer counts and the taxiway labels. Not written; the tests above use
  synthetic rows only. Owner: TBD.
- Procurement check for the `duckdb` Python library (#cop-procurement). Owner:
  TBD.
- Phase 2, Map designer `aeroway` layer (citymap union query). Owner: TBD. Per
  AGENTS.md, this MUST get a ticket with an owner; until the owner is named the
  follow-up has no ticket.
- Per-layer `source` and the Overture release in the API response meta, for
  support and for the ODbL / Overture attribution lines (see
  `docs/data-provider-terms.md` section 5). The data is on `AirfieldResult`
  only.
- Preview of a degraded render: store the SVG under its results token without
  the render cache, or accept the 404. Decision pending.
- Inline vs preview parity for the hybrid result: airports have no
  `artwork: "inline"` path in this repo, so no such test exists.
