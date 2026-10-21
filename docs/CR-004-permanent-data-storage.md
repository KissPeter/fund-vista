# CR-004 — fund-vista /data on permanent host storage (Mac)

- Status: done
- Created: 2026-10-21
- Last updated: 2026-10-21
- Type: change request
- Update (2026-10-21, CR-005): the NAS is decommissioned; the deploy script
  was renamed `ops/deploy-mac.sh` and the named-volume branch removed
  (the Mac's host directory is now the only `/data`).
- Decided: the prod container's `/data` lives on a host directory
  (`~/srv/fund-vista-data`) on the Mac's own disk instead of a Docker named
  volume inside the container engine's disk image. The NAS keeps the volume
  (it sets no `FV_DATA_DIR`).
- GitHub issue: #67 (claimed: `agent:in-progress`, assigned to KissPeter)

## Motivation

The Mac became the production host in CR-003, but its persistent store
stayed where the NAS had it: the Docker **named volume** `fundvista-data`,
i.e. inside the container engine's disk image on the Rancher Desktop VM.

That is not permanent storage. The host SSD filled up (127 MB free), the
engine's metadata DB was corrupted, and the VM had to be rebuilt — the
volume went with it. The store came back only because
`~/nas-migration/dumps/fundvista-data.tgz` existed. Meanwhile the traffic
ingest and the daily cron write into it continuously, so the same loss
would be routine rather than exceptional.

## Scope

- `ops/deploy-nas.sh` — new `FV_DATA_DIR` override. Unset → the named
  volume `fundvista-data` (the NAS is unchanged); set → `/data` is a bind
  mount of that host path and no volume is created. `data_source()` picks
  the source, `ensure_data()` prepares it, `migrate_data()` and
  `prod_run()` use it.
- `~/srv/ci/compose.yml` — the Mac CD sets
  `FV_DATA_DIR=/Users/kissp/srv/fund-vista-data`, so every CD deploy
  recreates the prod container against the host directory.
- Migration: the volume's content was copied to the host directory, the
  prod container was recreated against it, and the volume was removed.
- The CD runner now **copies `ops/deploy-nas.sh` before running it**: a
  deploy syncs the checkout to the pushed sha, which rewrites the very
  script it is executing (this is how the `FV_CPUS` change shipped with
  the old 3-CPU quota the first time round).

## The path-resolution trap

`docker run -v <src>` is resolved by the daemon **inside** the VM, not by
the client that runs the deploy. A host path only works if the VM can see
it. This Rancher Desktop is Lima-based and shares the Mac home over
virtiofs at the *same absolute path*
(`lima-… on /Users/kissp type virtiofs`), so
`/Users/kissp/srv/fund-vista-data` is a valid daemon-side bind source —
no translation needed. (The VZ backend's `/run/desktop/mnt/host/…` layout
does not exist on this host.)

The flip side: docker **auto-creates** a missing bind source, so a typo
would silently put the store back into the engine's disk — the exact
failure this change removes. `ensure_data` therefore probes the source
daemon-side and requires the `.fund-vista-data` marker file that the
operator puts in the real data directory; without it the deploy fails
loudly instead of creating a stray directory.

## Explicitly unchanged

- The store layout (`/data/traffic/day/<day>/<ICAO>.json.gz`,
  `/data/parsed`, `/data/results`, `/data/images`) and the git-carried
  slices (`backend/traffic/slices/`).
- `ops/deploy-nas.sh` behaviour on the NAS (same script, same defaults).
- The redis cache volume `fundvista-redis-data` stays on the engine disk:
  it is a regenerable cache (maxmemory 1 GB, `allkeys-lru`).

## DoD

- [x] `FV_DATA_DIR` override in `ops/deploy-nas.sh`; unset → named volume
      (NAS unchanged), set → host directory, no volume created.
- [x] Mac CD sets `FV_DATA_DIR`; the prod container's `/data` source is
      `/Users/kissp/srv/fund-vista-data` (verified with `docker inspect`).
- [x] Existing content migrated (35 MB: `images/`, `parsed/`, `results/`,
      `traffic/day/2026-10-17..19`) and served from the host directory.
- [x] The old `fundvista-data` volume removed; only the regenerable redis
      cache volume remains.
- [x] The CD runner copies the deploy script before running it, so a
      mid-run checkout rewrite cannot corrupt the running deploy.
- [x] Verified: `/v1/health` → ok (`redis: reachable`,
      `store_writable: true`), traffic availability for 2026-10-19 →
      `ready` (1.9 MB) after the switch.
- [x] Backend suite green (677 passed, 1 skipped; the one failure,
      `test_penplot_drawing.py::test_budget_overrun_returns_no_words_and_a_warning`,
      pre-dates this work and fails on a clean tree too — no Python was
      touched here).
- [x] Docs: this file, `docs/index.md`, GitHub issue #67.

## Follow-ups

- Move the redis cache volume to the host disk too, if a VM rebuild ever
  has to preserve a warm cache (it is regenerable, so this is optional).
- The daily ingest cron exports the slices to git, which is the portable
  third copy; a VM rebuild now costs nothing but the container itself.
