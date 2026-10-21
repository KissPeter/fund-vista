# CR-003 — Mac CD (webhook → deploy) + parallel multi-airport traffic ingest

- Status: done
- Created: 2026-10-20
- Last updated: 2026-10-20
- Type: change request
- Update (2026-10-21, CR-005): the NAS is decommissioned; the deploy script
  was renamed `ops/deploy-mac.sh` and the NAS-only volume/quotas dropped.
  The Mac is the only backend.
- Decided: the Mac is the deploy target (laminar retired locally); a main
  merge triggers one deploy pass via a GitHub push webhook; the traffic
  ingest parses the day archive in a process pool and serves every
  airport of the list from one archive pass.

## Motivation

Two problems, both visible only after the NAS → Mac migration:

1. **No auto-deploy on the Mac.** The CD chain was a GitHub push webhook →
   `ci.linuxadm.hu/hooks/github-ci` → `hook-github.sh` → laminar job
   `fund-vista` → `fund-vista-deploy` → `ops/deploy-nas.sh`. On the Mac
   the webhook only listened to `pull_request` (a main merge never
   arrived), the local `laminar-ci` container was dead, and the host SSD
   was full (127 MB free) — which is what corrupted the Docker VM's
   metadata store and made every mutating `docker` call fail.
2. **Traffic ingest pinned one core.** The pass streams ~4 GB of
   adsb.lol day archive and parses it in a single thread (gzip + json +
   Python point scans — GIL-bound), so the container sat at exactly
   100% of one core while its 5-CPU quota idled. One archive pass per
   airport also meant N airports = N × 4 GB downloads.

## Scope

### Deployment

- `~/srv/ci/hooks/hook-github.sh` — rewritten: a verified push to
  `refs/heads/main` of `KissPeter/fund-vista` queues the sha; a
  long-lived runner deploys it with the repo's own `ops/deploy-nas.sh`
  (build → candidate smoke → swap → live smoke/rollback). Serialized by
  a lock dir, stale runners are taken over, rapid pushes collapse into
  the next deploy.
- `~/srv/ci/compose.yml` — `laminar-ci` service removed; `ci-webhook`
  gains `docker.sock` + the prod checkout (`~/srv/fund-vista` mounted at
  `/opt/fund-vista`) and runs the deploy itself.
- GitHub webhook `685475003` — events `push` only (was `pull_request`).
- `ops/deploy-nas.sh` — `FV_CPUS` / `FV_MEMGB` env overrides for the
  prod container quota (NAS keeps 3.0/4; the Mac VM sets 5.0).

### Traffic ingest

- `POST /v1/jobs {type:"traffic_ingest", request:{icaos:[...]}}` next to
  the legacy `{icao: ...}` shape: one archive pass serves every airport
  of the list. `extract_multi` skips a doc for an airport whose bbox is
  disjoint from the doc's trace bbox — exact, not heuristic.
- `extract_multi(workers=N)` parses the archive in a `ProcessPoolExecutor`
  (`TRAFFIC_INGEST_WORKERS`, `0` = serial = the NAS default). Batches
  merge in archive order, so the result equals the serial pass.
- Classifier: traces with `null` altitudes no longer crash the pass
  (an unknown altitude proves neither low nor high). `CLASSIFIER_VERSION`
  `c2` → `c3`.
- `ops/traffic-ingest-daily.sh` — one multi-airport job per day, a
  rolling `TRAFFIC_DAYS` window (default 3, so a late-published archive
  is picked up later), a per-airport fallback for older apps, and BSD
  date support (macOS). Runs as launchd agent
  `hu.linuxadm.traffic-ingest` (08:10 + 20:10).

## Explicitly unchanged

- The public API of the single-airport job (`icao`, `movements`,
  `flights`, `bytes`, `cache_hit`, `attribution`) — the `icaos` shape
  adds a per-airport `airports` list next to it.
- `ops/deploy-nas.sh` behaviour on the NAS (same script, same defaults).
- The slice store layout (`/data/traffic/day/<day>/<ICAO>.json.gz`) and
  the git-carried slices (`backend/traffic/slices/`).
- The adsb.lol / OurAirports upstreams and their licensing terms.

## DoD

- [x] Live-server integration tests for the new job shapes (single
      unchanged, multi happy path, mixed cache hit, partial error, shape
      validation) — `backend/tests/test_traffic_api.py`.
- [x] Unit tests for the pure algorithm: bbox prefilter partitions and
      serial/parallel equivalence (the serial reader is sabotaged so a
      silent fallback fails) — `backend/tests/test_traffic_ingest_multi.py`.
- [x] Unit tests for the null-altitude classifier partitions —
      `backend/tests/test_traffic_phase1.py`.
- [x] Full backend suite green (677 passed; the one failure in
      `test_penplot_drawing.py` pre-dates this work and fails on a clean
      tree too).
- [x] End-to-end proof: 2026-10-19 harvested for all 57 open Hungarian
      airports (one pass, 57/57, 0 errors), slices exported to git and
      pushed by the daily script.
- [x] Docs: `docs/nas-cd.md` (deploy chain) and this file.

## Follow-ups

- Parallelise the *download* of the tar parts (the remaining serial
  stage — the network now bounds the pass).
- Per-batch progress counters in the job payload (the UI shows only
  `stage=download`).
- `py-spy` profile of the parent process to see how much of the pass the
  parent still owns (tar read + member shipping).
