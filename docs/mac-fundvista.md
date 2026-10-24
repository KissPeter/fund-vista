# fund-vista on the Mac — the pen-plot backend

Status: LIVE since 2026-10-20 (the NAS host is decommissioned). The shop
calls this backend same-origin (`/v1/*`); the VPS nginx routes to the Mac
with a FastAPI Cloud fallback.

## Topology

```
browser ──► https://pen-pixel.hu/v1/* ──► nginx (VPS) ──┬──► 127.0.0.1:8100 (reverse tunnel → Mac, container fundvista, 4 workers)
                                                        └──► @fundvista_cloud → https://fund-vista.fastapicloud.dev (fallback, 502/504 only)
```

- The Mac runs the backend in **Rancher Desktop** (Lima VM, 6 CPUs / 8 GB).
  Containers: `fundvista` (the app), `fundvista-redis` (cache),
  `ci-webhook` (the CD receiver).
- Source: `~/srv/fund-vista` — the CD checkout, left on the deployed sha
  (detached HEAD). The image is built from the repo-root `Dockerfile`
  (port/workers via env, `sh -c`/`${PORT}`), so the same image shape the
  FastAPI Cloud fallback runs.
- Env (in `~/srv/fund-vista/.env`, `600`, never in git): `PORT=8100`,
  `WORKERS=4`, `REDIS_CLOUD_URL=redis://fundvista-redis:6379/0`,
  `PENPLOT_PUBLIC_BASE_URL=https://penplot.linuxadm.hu` (absolute
  `svg_url`s stay same-origin), `PENPIXEL_HMAC_SECRET` (== cloud, HMAC
  verify cross-backend), `PENPLOT_TRUST_FORWARDED_FOR=1`,
  `PENPLOT_DATA_DIR=/data`.
- Container `fundvista`: `--network fundvista-net`,
  `-p 127.0.0.1:8100:8100`, `--cpus=5.0 --memory=4g`,
  `--restart unless-stopped`, `--env-file /opt/fund-vista/.env`.
- Store: `/data` is the host directory `~/srv/fund-vista-data`, bind-mounted
  (not a Docker volume — a volume died with the VM once already; see
  `docs/CR-004-permanent-data-storage.md`). Holds `images/`, `parsed/`,
  `results/`, `traffic/`.
- Redis `fundvista-redis` (`redis:7`, `--maxmemory 1gb
  --maxmemory-policy allkeys-lru`, volume `fundvista-redis-data:/data`) — a
  regenerable cache, so it stays on the VM disk.
- Tunnel: `~/Library/LaunchAgents/hu.linuxadm.tunnel-fundvista.plist`
  (autossh `-R 127.0.0.1:8100:127.0.0.1:8100 oc-tunnel@linuxadm.hu`) maps
  the VPS `127.0.0.1:8100` to the Mac.
- Nginx (VPS): `/etc/nginx/sites-enabled/pen-pixel.hu`, `location /v1/`
  (5 s connect, 310 s read/send — renders/imports/converts run up to
  ~300 s) + `@fundvista_cloud`. `X-Penplot-Upstream: mac|cloud` identifies
  the serving backend.
- Shop: `src/lib/penplot.api.ts` (same-origin `/v1/*`, `kp_*` mapping),
  `src/lib/v1proxy.ts` (dev/e2e), `PENPLOT_API_BASE=https://penplot.linuxadm.hu`.

## Pre-deploy gates (all mandatory)

1. Full backend suite green: `backend/.venv/bin/python -m pytest
   backend/tests/ -q` (and the GitHub Actions workflow must be green).
2. Changes committed on `main` and pushed (`git push origin main`).
3. The deploy is the gate: the swap only happens after the candidate
   container passed both smokes.

## Rebuild / update

The push to `main` deploys by itself (see `docs/mac-cd.md`). A manual
deploy:

```sh
sh ~/srv/fund-vista/ops/deploy-mac.sh            # origin/main tip
sh ~/srv/fund-vista/ops/deploy-mac.sh <sha>      # an exact commit
sh ~/srv/fund-vista/ops/deploy-mac.sh --verify   # smoke the live container
```

## Post-deploy verification

1. `docker ps --format '{{.Names}} {{.Status}}'` — `fundvista` Up,
   `fundvista-redis` Up, no restart loop (4 workers import numpy/cv2:
   ~30 s to healthy).
2. `docker logs --tail 20 fundvista` — startup line, no traceback.
3. `curl -s http://127.0.0.1:8100/v1/health` → `{"status":"ok",
   "redis":"reachable", ... "store_writable":true}`.
4. `launchctl list | grep hu.linuxadm.tunnel` — the tunnels are listed.
5. Public check: `curl -sD - https://pen-pixel.hu/v1/health` →
   `x-penplot-upstream: mac`.

## Failover drill / rollback

```sh
# VPS as root: public health must flip to cloud while the Mac app is stopped
docker stop fundvista
curl -sD - https://pen-pixel.hu/v1/health -o /dev/null | grep -i x-penplot-upstream
docker start fundvista
```

- Backend rollback: `docker stop fundvista && docker rm fundvista &&
  docker tag fundvista:mac-prev fundvista:mac` + re-run the same
  `docker run`; or revert the nginx vhost (`nginx -t && systemctl reload
  nginx`).
- A deploy that fails its live smoke rolls back by itself (previous image
  restarted and smoked, 90 s).

## Gotchas

- **The store is a host directory, not a volume.** `/data` =
  `~/srv/fund-vista-data`; the container's write path is the Mac's disk.
  The deploy probes the directory daemon-side for a `.fund-vista-data`
  marker, because docker auto-creates a missing bind source (which would
  silently put the store back inside the VM's disk).
- **A deploy swaps the container**, so an in-flight `traffic_ingest` job
  dies with it — the job lives in the container's memory.
- The prod checkout is left **detached** at the deployed sha; the daily
  ingest checks `main` out again before it commits.
- **The Mac is the only backend.** The old NAS host is decommissioned
  (unreachable); nothing falls back to it. FastAPI Cloud is the only
  fallback, and it has no traffic slices and no job state — so
  `/v1/traffic/*` must not fall back to it (a 502 there would surface as a
  confusing `job_not_found`).
- Image bytes are local per backend (content-addressed sha256): a design
  uploaded to the Mac converts only on the Mac. Per-request fallback is
  safe — same bytes re-upload to the cloud reproduce the same `image_id`,
  and the upload page self-heals one silent re-upload on convert-404.
  Token verify is pure HMAC + shared secret, so it works cross-backend.
- Rate limiting is per client IP via `X-Forwarded-For` (nginx sets it;
  backend `PENPLOT_TRUST_FORWARDED_FOR=1`). Backend default 100/min/IP
  shared across CPU endpoints.
- Host disk matters: the container engine's disk image lives on the Mac's
  disk too. A full disk (127 MB free once) corrupted the engine's
  metadata store and broke every mutating `docker` call — keep free space
  in the tens of GB.
- `svg_url`s are absolute: the Mac stamps
  `https://penplot.linuxadm.hu/v1/...`, the cloud stamps its own host. The
  shop download allow-list accepts both.
- Never include secrets in docs or commit messages; `.env` is gitignored
  and `git pull`/`git status` must stay clean of it.

## Airport traffic (Mac only)

- `/v1/traffic/*` and `traffic_ingest` jobs run on the Mac only (the ~4 GB
  archive pass needs the worker; the slices live in the store at
  `/data/traffic`). Nginx does NOT fall these paths back to FastAPI Cloud
  on 502/504 — the cloud has no slices and no job state, so the fallback
  would surface a confusing `job_not_found`. The vhost enforces it with
  `location ~ ^/v1/(traffic|jobs)(/|$)`, which keeps the upstream error
  visible; the shop UI shows "traffic temporarily unavailable" instead.
  This restriction goes away with Phase 7 (R2 bucket behind the same
  `SliceStore` keys + `TRAFFIC_STORE=r2`).
- Day slices live in git (`backend/traffic/slices/`, ~2 MB per airport-day)
  because the HU-only scope keeps them small. Daily ingest → push:
  `ops/traffic-ingest-daily.sh` (launchd agent
  `hu.linuxadm.traffic-ingest`, 08:10 + 20:10) POSTs `traffic_ingest` for
  yesterday (UTC) per `TRAFFIC_ICAOS` (default `LHBP`), polls to terminal,
  exports new files from the store into the repo checkout and pushes to
  `origin/main`. Idempotent (cache_hit days commit nothing); push failures
  retry on the next run.
- The ingest knobs live in the Mac's `.env` (gitignored, the deploy's
  `--env-file`): `TRAFFIC_INGEST_WORKERS` (archive parse in a process
  pool) and `TRAFFIC_INGEST_DOWNLOAD_WORKERS` (the tar parts fetched
  concurrently, `0` = serial). The archive is split into 2–3 parts, so
  `TRAFFIC_INGEST_DOWNLOAD_WORKERS=3` is the useful value; the parse
  workers and the download workers share the container's CPU quota, so
  raise them together (the container has 5 CPUs). See
  `docs/CR-007-parallel-archive-download-and-progress.md` for the
  measured split between the parent and the pool.
- The job document now carries real counters
  (`progress: {stage: "download"|"parse", done, total}`) — the 4 GB pass
  is observable via `GET /v1/jobs/{id}`. The shop-side UI does not render
  them yet (the UI lives in pen-pixel-shop).

---

Mirrors the operator runbook in `.opencode/skills/deploy-mac/SKILL.md` —
keep both in sync when anything here changes.
