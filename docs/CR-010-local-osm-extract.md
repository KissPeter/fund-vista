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

## Runbook (Mac mini)

Downloads are manual and untrusted data; keep them in their own directory.

```bash
mkdir -p ~/srv/osm-local/dl && cd ~/srv/osm-local/dl
curl -LO https://download.geofabrik.de/europe/hungary-latest.osm.pbf
curl -LO https://download.geofabrik.de/europe/hungary.poly
cd <repo>
uv run --project backend --with osmium python -m backend.osm_local.build \
  ~/srv/osm-local/dl/hungary-latest.osm.pbf ~/srv/osm-local/osm-hu.sqlite \
  --poly ~/srv/osm-local/dl/hungary.poly
```

`osmium` is a build-machine tool only (`--with`), not a project dependency,
so `uv.lock` and the licence inventory (CR-008) are untouched. The build
writes `*.tmp` and renames it, so a running service never sees a half file;
restart the service to pick up the new file.

Put the file on the Mac's data volume (the container sees `/data`) and set
`CITYMAP_LOCAL_OSM_DB=/data/osm-local/osm-hu.sqlite` on the backend. Check
`GET /v1/osm-local/health`. Refresh monthly by repeating download + build;
the new file is picked up without a restart.

## Licence

OSM data is ODbL 1.0: attribution is unchanged (`attribution` field of the
citymap API). Geofabrik extracts are redistributions of the same data.
