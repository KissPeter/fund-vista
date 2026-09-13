# Map data providers for Hungary / Europe (pen-plot city maps)

Date: 2026-09-13
Skill: `.opencode/skills/document-findings/SKILL.md`
Context: `/v1/citymap` failed with `overpass_unavailable` — all three
configured Overpass mirrors answered HTTP 504. Drawscape
(`https://drawscape.io/api/map-render`) uses Mapbox Streets v8 server-side
(see `docs/drawscape-map-source-findings.md`): faster/more reliable
(CDN-cached tiles vs live queries) but paid + token. This note lists
free/EU-friendly alternatives with the same OSM geometry.

## 1. Overpass API (current provider) — keep, tune, add EU mirrors

- What: live Overpass QL over OSM DB. Our query: union of
  `way[...](bbox)` + `relation[...](bbox)` per layer, `out body; >; out skel qt;`.
- Configured: `overpass-api.de`, `overpass.kumi.systems`,
  `overpass.private.coffee` (all 504'd at once — typical when the query
  itself is heavy, e.g. `buildings` over a large bbox, not just an outage).
- Extra EU mirrors worth adding (same API, different operators):
  - `https://overpass.openstreetmap.fr/api/interpreter` (OSM-FR, France)
  - `https://overpass.nchc.org.tw/api/interpreter` (NCHC, Taiwan — useful
    when EU instances are busy)
  - Status overview: OSM wiki "Overpass / public instances".
- Limits/policy: shared public resource; long `timeout:` (ours is 180s)
  hurts — keep queries small, lower timeout to ~25s, split `buildings`
  into its own request, prefer `out geom` over full recursion.
- Cost: free, no key. Attribution: `© OpenStreetMap contributors, ODbL`.
- Reproduce free:
  ```bash
  curl -X POST 'https://overpass-api.de/api/interpreter' \
    --data-urlencode 'data=[out:json][timeout:25];way["highway"](46.5189,16.7402,46.5635,16.7824);out geom 10;'
  ```

## 2. OpenFreeMap vector tiles (recommended Drawscape-like replacement, free)

- What: weekly Planetiler build, **unmodified OpenMapTiles schema**, served
  as static `.pbf` files over HTTP. Same architectural win as Mapbox
  (pre-generated, CDN-cached) with no key.
- Endpoints:
  - `https://tiles.openfreemap.org/planet/latest` (TileJSON, always latest)
  - `https://tiles.openfreemap.org/planet/latest/{z}/{x}/{y}.pbf`
  - Pinned snapshot form: `.../planet/20260513_001001_pt/{z}/{x}/{y}.pbf`
  - Full planet downloads (Btrfs/MBTiles, weekly): `https://btrfs.openfreemap.com`
- Cost/limits: public instance free, no registration, no API keys, no
  cookies, "no limits on views/requests" (donation-funded, run by
  Hyperknot Software Kft., Hungary). No SLA — self-host the MBTiles for
  hard guarantees. Provides tiles only (no geocoding/routing).
- Layer mapping (ours -> OpenMapTiles):
  | UI / payload | Native tile layer | Notes |
  |---|---|---|
  | `highways`,`roads` | `transportation` (`class`: motorway/trunk/primary/secondary/tertiary/minor/service/track) | Split `highways` vs `roads` client-side by `class` |
  | `paths` | `transportation` (`class`: path/pedestrian/cycleway) + `transportation_name` | |
  | `rails` | `transportation` (`class`: transit/rail) | |
  | `water` | `water` | Lakes/reservoirs polygons |
  | `waterway` | `waterway` | Rivers/streams/canals lines |
  | `buildings` | `building` | z14+ has full footprints |
  | `aeroway` | `aeroway` | Runways/taxiways/aprons |
  | `ferry` | `transportation` (`class`: ferry) | Route lines |
- Reproduce free (Zalaegerszeg z14 tile):
  ```bash
  curl -o tile.pbf 'https://tiles.openfreemap.org/planet/latest/14/8804/5762.pbf'
  python3 -c "import mapbox_vector_tile; d=open('tile.pbf','rb').read(); print(mapbox_vector_tile.decode(d).keys())"
  ```
- Caveat: needs an MVT decoder dep (e.g. `mapbox-vector-tile`) and
  client-side bbox clipping; buildings need z14–15 fetch loops.

## 3. Geofabrik Hungary extract (most reliable for HU, offline)

- What: daily OSM snapshot cut to Hungary. No request-time upstream —
  download once, query locally.
- Files (EU-hosted, updated daily):
  - `https://download.geofabrik.de/europe/hungary.html`
  - `hungary-latest.osm.pbf` (~308 MB, 2026-09-03 state at check time)
  - Also `.shp.zip`, `.gpkg.zip`, `.poly`, experimental Shortbread MVT package
- Cost: free (no user names/IDs — GDPR-stripped; full-metadata files are
  contributors-only). Tooling: `osmium`, `osmconvert -b=`, `pyrosm`,
  `osm2pgsql`.
- Reproduce free:
  ```bash
  curl -O https://download.geofabrik.de/europe/hungary-latest.osm.pbf
  osmconvert hungary-latest.osm.pbf -b=16.74,46.51,16.79,46.57 -o=zala.osm
  ```
- Best for: batch/poster jobs and a self-hosted fallback; overkill as a
  per-request online API.

## 4. OSM Main API v0.6 `/map` (zero-dep online fallback)

- What: main OSM database bbox read, OSM XML out. Different infra from
  Overpass, so it survives Overpass outages.
- Endpoint: `GET https://api.openstreetmap.org/api/0.6/map?bbox=west,south,east,north`
- Limits: bbox area max **0.25 deg²**, ~50k nodes guidance; API usage policy
  says the editing API is for editing, not bulk reads — keep requests small
  and cached. Chunk our `max_bbox_deg: 0.8` bboxes into tiles and merge.
- Reuse: convert OSM XML nodes/ways to our `split_elements` input with
  `defusedxml` (already a dependency). Same tag filters as Overpass.
- Reproduce free:
  ```bash
  curl 'https://api.openstreetmap.org/api/0.6/map?bbox=16.74,46.5189,16.7824,46.5635' -o zala.osm
  ```

## 5. ohsome API, Heidelberg (separate cluster, GeoJSON out)

- What: Heidelberg Institute (HeiGIT) OSHDB-backed API. Independent of
  Overpass — a true second source for live queries.
- Endpoint: `POST https://api.ohsome.org/v1/elements/geometry`
  params: `bboxes=west,south,east,north`, `time=2026-01-01`,
  `filter=building=* and geometry:polygon`, `properties=tags`, `clipGeometry=true`.
- Cost: free for fair use, no key for basic queries. Output GeoJSON —
  convert polygons/lines to our lon/lat lists, bypassing `split_elements`.
- Filter mapping: `highway=motorway,trunk,primary`,
  `railway=rail,light_rail,tram`, `natural=water`, `building=*`, etc.
- Reproduce free:
  ```bash
  curl -X POST 'https://api.ohsome.org/v1/elements/geometry' \
    -d 'bboxes=16.74,46.5189,16.7824,46.5635' -d 'time=2026-01-01' \
    --data-urlencode 'filter=building=* and geometry:polygon'
  ```

## 6. Paid / key-required (for completeness, not recommended now)

- **Mapbox Streets v8** (what Drawscape uses): `mapbox://mapbox.mapbox-streets-v8`,
  `.../v4/mapbox.mapbox-streets-v8/{z}/{x}/{y}.mvt?access_token=...`.
  Token + pay-as-you-go (~200k tiles/mo free, then ~$0.25/1k; verify at
  `https://www.mapbox.com/pricing`). Same OSM geometry for Hungary.
- **Stadia Maps** (incl. Stamen Toner, EU endpoints for GDPR):
  `https://tiles.stadiamaps.com/...?api_key=...`. Free tier is
  dev/eval/non-commercial; commercial from ~$20/mo. Key required.
- **MapTiler**: OpenMapTiles hosting with key, EU infra option. Key required.

## Recommendation for fund-vista

1. Short term: tune Overpass (25s timeout, split `buildings`, add
   `overpass.openstreetmap.fr` mirror) + chunked **OSM Main API fallback**
   (zero new deps).
2. Medium term: **OpenFreeMap tiles fallback** (`planet/latest/{z}/{x}/{y}.pbf`,
   z14–15, `transportation/water/waterway/building/aeroway` mapping) — the
   free version of Drawscape's architecture.
3. Keep **Nominatim** for geocoding (1 req/s policy, Redis-cached); keep
   **Geofabrik Hungary** as the offline/batch source.
4. Attribution everywhere: `© OpenStreetMap contributors · ODbL 1.0 ·
   https://osm.org/copyright` (already in API responses).
