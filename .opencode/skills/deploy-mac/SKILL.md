---
name: deploy-mac
description: Deploy the fund-vista backend to the Mac (rebuild fundvista:mac, recreate container, verify). Use when asked to deploy, release, or roll out backend changes to production/the Mac host.
---

# Deploy Backend to the Mac

The only production backend. The VPS nginx only proxies; all `/v1` work
runs on the Mac (Rancher Desktop VM), with a FastAPI Cloud fallback on
502/504.

## Topology (fixed)

- **Host**: the Mac itself. Containers: `fundvista` (app), `fundvista-redis`
  (cache), `ci-webhook` (CD receiver). The container engine is Rancher
  Desktop (Lima VM, 6 CPUs / 8 GB).
- **Repo**: `~/srv/fund-vista` (`github.com:KissPeter/fund-vista.git`) — the
  CD checkout, left on the deployed sha (detached HEAD). Untracked, never
  commit: `.env` (secrets, `600`).
- **Image**: `fundvista:mac` (rolling) / `fundvista:mac-<short>` (per
  deploy) / `fundvista:mac-prev` (rollback spare), built from the repo-root
  `Dockerfile` (port/workers via env, `sh -c`/`${PORT}`).
- **App container** `fundvista`: `--network fundvista-net`,
  `-p 127.0.0.1:8100:8100`, `--cpus=5.0 --memory=4g`,
  `--restart unless-stopped`, `--env-file /opt/fund-vista/.env`.
  Key env (in `.env`): `PORT=8100`, `WORKERS=4`,
  `REDIS_CLOUD_URL=redis://fundvista-redis:6379/0`,
  `PENPLOT_PUBLIC_BASE_URL=https://pen-pixel.hu`,
  `PENPLOT_DATA_DIR=/data`.
- **Store** `/data`: the host directory `~/srv/fund-vista-data`,
  bind-mounted (NOT a Docker volume — a volume lives in the engine's disk
  image and died with the VM once; see `docs/CR-004-permanent-data-storage.md`).
  Holds `images/`, `parsed/`, `results/`, `traffic/`.
- **Redis** `fundvista-redis` (`redis:7`, `--maxmemory 1gb
  --maxmemory-policy allkeys-lru`, volume `fundvista-redis-data:/data`) —
  regenerable cache, stays on the VM disk.
- **Tunnels** (launchd agents, `~/Library/LaunchAgents/hu.linuxadm.*`):
  `tunnel-fundvista` (autossh `-R 127.0.0.1:8100:127.0.0.1:8100
  oc-tunnel@linuxadm.hu`) maps the VPS `:8100` to the Mac, where nginx
  serves it as `https://pen-pixel.hu/v1/`; `tunnel-ci-webhook` (`:8290`)
  carries the GitHub push webhook back to the Mac.
- **CD**: a push to `main` deploys by itself — webhook → `ci-webhook` →
  `~/srv/ci/hooks/hook-github.sh` → `ops/deploy-mac.sh <sha>`
  (build → candidate smoke → swap → live smoke/rollback). See
  `docs/mac-cd.md`.

## Pre-deploy gates (all mandatory)

1. Full backend suite green: `backend/.venv/bin/python -m pytest
   backend/tests/ -q` (and the GitHub Actions workflow green).
2. Changes committed on `main` and pushed (`git push origin main`) — the
   push is the deploy trigger.

## Deploy steps

Normally: push to `main` and let the CD do it. Manual:

```sh
sh ~/srv/fund-vista/ops/deploy-mac.sh            # origin/main tip
sh ~/srv/fund-vista/ops/deploy-mac.sh <sha>      # an exact commit
sh ~/srv/fund-vista/ops/deploy-mac.sh --verify   # smoke the live container
```

The script syncs the checkout, builds the image, smokes a candidate
container (`fundvista-next`, internal only), swaps, smokes the live
container and rolls back to the previous image if that smoke fails.

## Post-deploy verification

1. `docker ps --format '{{.Names}} {{.Status}}'` — `fundvista` Up,
   `fundvista-redis` Up, no restart loop.
2. `docker logs --tail 20 fundvista` — startup line, no traceback.
3. `curl -s http://127.0.0.1:8100/v1/health` → `"status":"ok"`,
   `"redis":"reachable"`, `"store_writable":true`.
4. `launchctl list | grep hu.linuxadm.tunnel` — the tunnels are listed.
5. Public check: `curl -sD - https://pen-pixel.hu/v1/health` →
   `x-penplot-upstream: mac`.
6. Rollback if broken: `docker stop fundvista && docker rm fundvista &&
   docker tag fundvista:mac-prev fundvista:mac` + re-run the same
   `docker run` (flags in `docs/mac-cd.md`).

## Cautions

- `~/srv/fund-vista/.env` is host-local and untracked — never `git add` it.
- **A deploy swaps the container**, so an in-flight `traffic_ingest` job
  dies with it (the job lives in the container's memory).
- The prod checkout is left **detached** at the deployed sha; the daily
  ingest checks `main` out again before it commits.
- The bind source for `/data` is resolved by the daemon inside the VM, and
  docker auto-creates a missing one — the deploy probes the directory for
  its `.fund-vista-data` marker and fails loudly instead of creating a
  stray directory inside the engine.
- Host disk matters: the engine's disk image is on the Mac's disk too. A
  full disk (127 MB free once) corrupted the engine's metadata store and
  broke every mutating `docker` call.
- Traffic day slices live in git (`backend/traffic/slices/`): the launchd
  agent `hu.linuxadm.traffic-ingest` (08:10 + 20:10) runs
  `ops/traffic-ingest-daily.sh` (ingest yesterday → export from the store
  → commit → push to main).
- The Mac is the only backend; FastAPI Cloud is the only fallback, and it
  has no traffic slices and no job state — `/v1/traffic/*` must not fall
  back to it.
- Never include secrets in docs, skills, or commit messages.
