# F-008 Airfield hybrid source (Overture + Overpass)

Status: design, not implemented. The key words MUST, SHOULD and MAY are to be
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

- Both sources run concurrently; the render waits for both, bounded by the
  existing Overpass budget. An Overture failure MUST fall back to the full
  Overpass query, so behaviour never gets worse than today.
- Shared filters, applied to both sources by one function:
  - `around` semantics: keep a feature when its nearest point is within the
    radius (circle, not bbox).
  - drop `runway` ways shorter than 50 m (closed stubs such as LHBP `03/21`).
- Overture reader: DuckDB with `httpfs` and `spatial`, bbox pushdown on the
  parquet `bbox` struct, then the circle filter. Release pinned in config
  (`AIRPORTS_OVERTURE_RELEASE`). Old releases are removed upstream, so a
  startup check MUST resolve the latest release from the STAC catalog when the
  pinned one 404s.
- Labels come from `source_tags.ref` on `class=taxiway`; same designators as
  `extract_taxiway_refs`.
- Caching: Redis, per source and layer set, same TTL policy as Overpass
  payloads. The key MUST include the Overture release. Cold Overture is about
  7 s, so a worker cache hit matters more than DuckDB's in-process cache.
- Provenance: the response meta SHOULD record `source` per layer
  (`overture` or `osm`) and the Overture release, for support and for the
  ODbL / Overture attribution lines (see `docs/data-provider-terms.md` section 5).

## Impact

| Feature | Code | Change |
|---|---|---|
| Airport designer | `backend/airports/fetching.py` (`build_airport_query` call, ~line 108) | Replace with `sources.load_airfield`. Context layers (highways, water, buildings) stay on Overpass `build_context_query`. |
| Airport traffic | `backend/traffic/basemap.py` `_fetch_layer("airfield")`, radius 6000 m | Use `sources.load_airfield`; keep runway/taxiway/apron/terminal. Bump cache namespace `osm2` to `osm3`. Other basemap layers (country, motorways, waterways, water) are unchanged. |
| Map designer (citymap) | `backend/citymap/layers.py` `aeroway` = `way["aeroway"]({bbox})` inside one unioned query | Phase 2. The layer is a member of a union query, so splitting it changes the cache key and the single-query guarantee. Needs its own change. |
| 3D city view | `pen-pixel-shop`, not this repo: `BlueprintPreview.tsx`, route `/en/custom-design/city-view` (internal name `blueprint`). Client-side MapLibre + deck.gl on OpenFreeMap vector tiles; no `/v1/blueprint/render`. The plot SVG goes through the existing upload path (`/v1/images`, `/v1/tokens`, `/v1/convert`). | None. It never calls Overpass, so this change does not touch it. Only OSM/OpenFreeMap attribution stays as is. |

`POST /v1/compose-sheet` MUST remain the only sheet composer and keep one code
path for preview, personalisation and order file (AGENTS.md). This change
only swaps the geometry input. Preview and `artwork: "inline"` MUST receive
the same hybrid result, so placement stays identical; a test MUST assert it.

## Tests

- Golden LHBP fixture: Overture rows (recorded) and Overpass elements; assert
  per-layer counts and the 67 taxiway labels.
- Parity test: shared filter applied to both sources, id-matched.
- Failover: Overture down uses full Overpass; Overpass down gives the Overture
  layers plus `osm_layers_unavailable`; both down keeps today's error path.
- Inline vs preview parity for the hybrid result.

## Risks

- Overture lags OSM by up to a release cycle (monthly): a newly built or closed
  runway shows late. Acceptable for posters; MUST be stated in the meta.
- New dependency: the `duckdb` Python library, embedded in the backend process
  (no server, no daemon, nothing to run). Its `spatial` and `httpfs` extensions
  download on first use, so the container image MUST preinstall them. Check
  procurement before adding (#cop-procurement).
- S3 egress from the host and a cold start of about 7 s per airport.

## Phases and tickets

1. Airport designer and airport traffic (shared fetcher).
2. Map designer `aeroway` layer.

The 3D city view needs no phase (see Impact).

Per AGENTS.md, phase 2 MUST get a ticket with an owner in the same change that
lands phase 1. Owner: TBD, to be named by the project owner.
