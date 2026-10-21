# Continuous delivery: fund-vista push → Mac deploy

Push to `origin/main` deploys itself to the `fundvista` container on the
Mac (build + two smoke gates) within a few minutes. The Mac is the only
backend; the VPS nginx keeps a FastAPI Cloud fallback for 502/504.

## Chain

1. GitHub `push` webhook → `POST https://ci.linuxadm.hu/hooks/github-ci`
   (repo webhook id `685475003`, push only, secret in `~/srv/ci/.env`).
2. The VPS nginx forwards it to `127.0.0.1:8290`, which the Mac's reverse
   tunnel (`~/Library/LaunchAgents/hu.linuxadm.tunnel-ci-webhook.plist` —
   autossh `-R 127.0.0.1:8290:127.0.0.1:8290 oc-tunnel@linuxadm.hu`) maps
   back to the Mac.
3. The `ci-webhook` container (`~/srv/ci/compose.yml`) verifies the
   `X-Hub-Signature-256` HMAC and runs `~/srv/ci/hooks/hook-github.sh`.
4. The hook queues the sha in `~/srv/ci/cd/lock/want`; a long-lived runner
   picks it up and runs `ops/deploy-mac.sh <sha>`. One deploy at a time
   (lock dir), rapid pushes collapse into the next deploy, a stale runner is
   taken over.
5. The deploy syncs the prod checkout (`~/srv/fund-vista`, mounted at
   `/opt/fund-vista` inside the container), builds the image, smokes a
   candidate container, swaps it, smokes the live one and rolls back on
   failure.

There is no laminar on the Mac: the test gate is GitHub Actions
(`.github/workflows/`), the Mac only deploys. The prod checkout is left on
the deployed sha (detached HEAD) — the daily traffic ingest checks `main`
back out before it commits (see `docs/CR-005-mac-only-deployment.md`).

## What a deploy does (`ops/deploy-mac.sh`)

1. Disk gate (≥2G free on the VM disk), fetch + checkout the exact `<sha>`
   (refuses a dirty tree; `main` mode fast-forwards ff-only).
2. Idempotence: if the live container already carries the label
   `fundvista.commit=<sha>`, exit after prune.
3. `docker build -t fundvista:mac-<short>` (reuses the tag when the image
   already exists), previous image kept as `fundvista:mac-prev`.
4. `/data` is the host directory `~/srv/fund-vista-data`, bind-mounted into
   the container — never a Docker volume, which lives inside the container
   engine's disk image and is lost on a VM rebuild. A marker file
   (`.fund-vista-data`) in that directory is probed daemon-side, so a typo
   cannot make docker auto-create a stray directory inside the engine.
5. Candidate `fundvista-next` (`WORKERS=1`, internal only, no published
   ports) → smoke: `/healthz` answers, `/v1/health` is 200 with
   `"status":"ok"`. On failure the live container is untouched.
6. Swap: stop/rm `fundvista`, start the new image with the production flags
   (`--network fundvista-net --restart unless-stopped
   -p 127.0.0.1:8100:8100 --cpus=5.0 --memory=4g
   -v /Users/kissp/srv/fund-vista-data:/data
   --env-file /opt/fund-vista/.env`), then smoke the live container.
7. Rollback: if the live smoke fails, the previous image is started again
   with the same flags and smoked (90 s). Public traffic falls back to
   FastAPI Cloud via the VPS nginx while this happens
   (see `docs/mac-fundvista.md`), so a double failure is an outage of the
   primary only, never of the public site.
8. Prune: keep the newest 3 `fundvista:mac*` images (live + rollback +
   spare); never force-removes an image a container uses (`docker rmi`
   fails cleanly there).

## Operating

- Watch a deploy: `tail -f ~/srv/ci/cd/logs/deploy-*.log` (one log per
  deploy); the image build log lands in `~/srv/ci/cd/build-logs/`.
- Manual deploy of the tip: `sh ~/srv/fund-vista/ops/deploy-mac.sh`
  (refuses dirty/diverged trees).
- Manual deploy of a sha: `sh ~/srv/fund-vista/ops/deploy-mac.sh <sha>`.
- Verify only: `sh ~/srv/fund-vista/ops/deploy-mac.sh --verify`.
- Re-run a deploy by hand: write the sha into
  `~/srv/ci/cd/lock/want` — the runner picks it up within seconds.
- Rollback by hand: `docker stop/rm fundvista` + `docker run` with the
  `fundvista:mac-prev` tag and the §6 flags, then `--verify`. No data
  migration is involved (the store is a host directory, the content is
  content-addressed + HMAC tokens), so rollback is just the swap.
- Disable CD (keep manual deploys): stop the receiver —
  `docker compose -f ~/srv/ci/compose.yml stop webhook` — or delete the
  GitHub webhook. `ops/deploy-mac.sh` keeps working standalone.

## Secrets and paths (never in git)

- `~/srv/fund-vista/.env` (`600`) — runtime env, read by the deploy for
  `--env-file`. When unreadable the deploy inherits the live container's
  inspected env instead (and refuses without `PENPIXEL_HMAC_SECRET`).
- `~/srv/ci/.env` — `WEBHOOK_SECRET` (the GitHub webhook HMAC key) and
  `GH_TOKEN` (the deploy's git fallback when no SSH key is available).
- `~/srv/fund-vista` — prod checkout (CD leaves it on the deployed sha).
- `~/srv/fund-vista-data` — the store (`/data`): `images/`, `parsed/`,
  `results/`, `traffic/day/<day>/<ICAO>.json.gz`. On the Mac's own disk, so
  a container-engine/VM rebuild costs the container, not the store.
- Image tags live in the local docker store only: `fundvista:mac`
  (rolling = live), `fundvista:mac-<short>` (per deploy),
  `fundvista:mac-prev` (rollback spare).
