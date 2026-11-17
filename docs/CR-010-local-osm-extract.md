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

- `backend/osm_local/` answers **only** the query shape citymap sends
  (`overpass.wrap_query`) at `POST /api/interpreter`, plus `GET /health`.
  It needs no change to the citymap pipeline: the URL is simply the first
  mirror (`CITYMAP_LOCAL_OSM_URL`, `overpass.mirror_urls()`).
- Anything it cannot answer faithfully — another query shape, a filter not
  derived from `layers.py`, a stale database, or a bbox not **entirely**
  inside the extract polygon — is a `400`. The shared client fails over on
  400 without parking the mirror, so those requests reach the public
  mirrors exactly as before. A dead/unreachable service is parked for 30 s
  by the same client.
- Coverage is the Geofabrik `.poly`, not a rectangle: a rectangle around
  Hungary contains Bratislava and Vienna, which the extract does not hold.
- Storage: ways keep node ids + 1e-7° coordinates inline (no node table),
  an R*Tree for space and a selector bitmask per way/relation. Rebuild
  after `layers.py` selector changes (`/health` reports `stale`; the
  service then answers 400 until rebuilt).

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

Service: install `ops/osm-local.plist` (header has the commands), then set
on the backend `CITYMAP_LOCAL_OSM_URL=http://host.docker.internal:8200/api/interpreter`
(Rancher Desktop; the service binds `127.0.0.1` by default — use
`OSM_LOCAL_HOST=0.0.0.0` only if the container cannot reach it, and then
firewall port 8200). Check: `curl localhost:8200/health`.

Refresh monthly (or weekly) by repeating the download + build, then
`launchctl kickstart -k gui/$(id -u)/hu.linuxadm.osm-local`.

## Licence

OSM data is ODbL 1.0: attribution is unchanged (`attribution` field of the
citymap API). Geofabrik extracts are redistributions of the same data.
