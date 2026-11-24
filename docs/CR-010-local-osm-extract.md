# CR-010 — Local OSM extract for Hungary (citymap speed)

- Status: done
- Created: 2026-11-17
- Last updated: 2026-11-17
- Type: change request
- Decided: Planet.osm itself is not used (≈80 GB PBF, weekly, no query API).
  A Geofabrik **Hungary** extract lives on the Mac mini as a SQLite file
  (~1–2 GB) behind a tiny Overpass-compatible service; everything outside
  Hungary still comes from the public Overpass mirrors.

## Why

`/v1/citymap` is slow when its cache is cold: every uncached tile is a
multi-second round trip to a shared public Overpass mirror, and heavy
queries 504. Local answers take milliseconds and have no rate limit.

## Design

- Part of fund-vista, not a second app. `backend/osm_local/` is mounted in
  `main.py` and exposes the **same API as a public Overpass mirror**:
  `POST /v1/osm-local/api/interpreter` (form field `data`, Overpass JSON),
  plus `GET /v1/osm-local/health`.
- Citymap prefers it: `fetch_overpass` asks the same code in-process
  (`service.fetch_local`, no HTTP hop) before the public mirrors. Config is
  one path, `CITYMAP_LOCAL_OSM_DB` (empty = off).
- It answers **only** the query shape citymap sends (`overpass.wrap_query`).
  Anything it cannot answer faithfully — another query shape, a filter not
  derived from `layers.py`, a stale database, or a bbox not **entirely**
  inside the extract polygon — is declined (`None` in-process, `400` over
  HTTP, like a mirror refusing a query) and the request goes to the public
  mirrors exactly as before.
- The file is re-opened when replaced, so a rebuild needs no restart.
- Coverage is the Geofabrik `.poly`, not a rectangle: a rectangle around
  Hungary contains Bratislava and Vienna, which the extract does not hold.
- Storage: ways keep node ids + 1e-7° coordinates inline (no node table),
  an R*Tree for space and a selector bitmask per way/relation. Rebuild
  after `layers.py` selector changes (`/health` reports `stale`; it is
  then declined until rebuilt).

## Early length filter ("Detail" / `min_path_len_m`)

`render_svg` drops polylines shorter than `min_path_len_m` last, after
everything was fetched and split. `citymap/shortways.py` drops them where
elements enter our code instead:

- the value is floored to a bucket (0, 5, 10, 25, 50, 100 m; threshold =
  bucket × 0.95) so the early filter never removes a way the renderer keeps;
  the exact filter in `render_svg` still runs last;
- the bucket is part of the tile and split cache keys (`tile-v3`,
  `split-v2`), so a filtered payload only serves requests in its bucket;
- local extract: `ways.len_m` (schema 2) filtered in SQL, so short ways are
  never read; a schema 1 file keeps working and is post-filtered;
- public Overpass / OSM API: filtered right after parsing (a server-side
  `if: length()` was slower on the public mirror, measured 2026-11-17).

Measured on Budapest 0.1x0.11 deg, all layers (laptop, local extract): raw
ways 92k / 85k / 72k / 49k at 0 / 10 / 25 / 50 m, peak RSS 981 / 1025 / 886 /
820 MB. The default 10 m removes only ~8% of ways, so the memory win is small
there; most of the ~900 MB is the Overpass-shaped element dicts themselves
(~1.6 KB per element), not the short ways.

## Compact elements (memory)

A dense all-layer render held ~560k Overpass-shaped dicts (~1.6 KB each,
~900 MB) and got workers OOM-killed in the 4 GB container (kernel log:
`Memory cgroup out of memory: Killed process (uvicorn) anon-rss:3.3GB`).
Elements are now *compact* from the fetch boundary on
(`shortways.compact_elements`): a way carries its geometry inline,
`g = [lon, lat, lon, lat, ...]`, and node dicts do not exist. The extract
builds them directly (`Store.run(..., compact=True)`, no node dicts at all);
public Overpass / OSM API answers are converted right after parsing. Tile
payloads in Redis are compact too (`tile-v4`: no per-tile node duplication).
Node-form elements still work everywhere (`split_elements`, tiles, clip), so
mixed input is safe.

Measured on Budapest 0.1x0.11 deg, all layers, same SVG byte for byte
(sha256 identical, 10 m and 50 m): elements 560k -> 86k, peak RSS 884 -> 424
MB at the default 10 m (733 -> 356 MB at 50 m), load 6.2 -> 4.6 s.

## Airports and airport traffic (same extract, same code)

Airports (`backend/airports`) and airport traffic (`backend/traffic`, whose
airfield layer calls the same function) ask Overpass for `around` queries
answered with `out geom`. They go through `fetch_airport_polygons`, which now
asks `osm_local.service.fetch_local_geom` first - the same store, per-worker
cap, coverage check and fall-through rules as citymap. One implementation:
`ql.parse_around` (parser), `Store.run_around` (R-tree box, then exact
distance to the way, `out geom` JSON), `selectors.AROUND_FILTERS` (which
stored selectors stand in for each filter text). The HTTP interpreter answers
the same shape.

The country-scale traffic basemap uses the same entry point. Traffic's bbox
`out geom` queries are parsed by `ql.parse_geom` (single way or union) and
answered by `Store.run_geom` via `selectors.BBOX_FILTERS` when every filter has
a stored selector:

- motorways: `way["highway"~"^(motorway|trunk)$"]`;
- waterways: `way["waterway"="river"]["name"]`;
- water: `way`/`relation["natural"="water"]["name"]["water"~"^(lake|reservoir)$"]`.

A traffic day-slice bbox is much larger than Hungary, so these queries do
**not** use citymap's full-coverage check. `coverage.clip_bbox` (exposed as
`Store.clip_bbox`) intersects the requested bbox with the extract coverage
rectangle and `answer()` queries only that part. An empty intersection (or an
unsupported filter, or a stale store) still falls through to the mirrors; citymap
and `around` queries keep their strict full-coverage contract. Clipped answers
are complete only inside the covered area - the traffic map is Hungary-only,
so that is the intended behaviour.

The stored selector bits are wider than these filters, so `run_geom`
re-checks each candidate's tags: primary roads, streams, unnamed rivers and
ponds/reservoir-less water are not returned. No new registry selector (and no
database rebuild) is needed. The **country border** query
(`relation["boundary"="administrative"]["admin_level"="2"]` with
`ISO3166-1=HU` / `name=Magyarország`) has no selector in the extract, so it
still goes to Overpass and is served from the L3 basemap cache. The 3D city
view is not part of this repo and never calls Overpass.

- `["highway"]`-style filters are wider than the registry: ways the registry
  never stores (`highway=construction`) are omitted. The airport splitters drop
  them anyway; `test_every_class_the_airport_splitters_draw_is_stored_locally`
  pins that.
- `relation["aeroway"]` is an *extra* selector appended after the citymap ones.
  A database built before it existed stays valid (`Store.fresh` accepts a
  stored registry that is a prefix) and keeps serving citymap, but airport
  queries fall through to the mirrors until the next rebuild
  (`launchctl kickstart gui/$(id -u)/hu.linuxadm.osm-local-refresh`, about 2 h).
- Checked against `overpass-api.de` for Budapest (LHBP, 3 km): the aeroway ways
  are identical (285 ids, 0.16 s vs 37 s); the context layers differ by about
  1 percent in count (the extract is newer than the mirror's snapshot).

## Runbook (Mac mini)

No new volume: the container's `/data` is already bind-mounted from
`FV_DATA_DIR` on the Mac's disk, so the database lives at
`$FV_DATA_DIR/osm-local/osm-hu.sqlite` = `/data/osm-local/osm-hu.sqlite`.
`ops/deploy-mac.sh` sets `CITYMAP_LOCAL_OSM_DB` to that path on every
deploy (override with `FV_OSM_DB`). A missing file is harmless: citymap
simply keeps using the public mirrors.

First run, and the monthly refresh, on the Mac host (downloads Geofabrik
`hungary-latest.osm.pbf` ≈ 330 MB + `hungary.poly` into a temp dir, builds
in ~3 min, ~1.3 GB, then swaps the file atomically):

```bash
ops/osm-local-refresh.sh
```

The script builds from a clean export of `origin/main`, so the Mac checkout's branch does not matter. The running backend picks the new file up without a restart. Check
`GET /v1/osm-local/health` (`ok` / `stale` / `off`). `osmium` is a
build-machine tool only (`uv run --with osmium`), not a project
dependency, so `uv.lock` and the licence inventory (CR-008) are untouched.

Scheduled refresh: `ops/osm-local-refresh.plist` (launchd agent
`hu.linuxadm.osm-local-refresh`, 1st of the month 03:00; install steps in its
header; log `~/Library/Logs/osm-local-refresh.log`). A failed run keeps the
previous file. Rebuild by hand after changing selectors in `layers.py`.

Measured 2026-11-17 on real data: 5.16 M ways, 1.28 GB, 173 s, 1.1 GB RAM;
Budapest all-layer query 469 ms (public Overpass timed out at 30 s).

## Licence

OSM data is ODbL 1.0: attribution is unchanged (`attribution` field of the
citymap API). Geofabrik extracts are redistributions of the same data.
