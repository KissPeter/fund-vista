# Continuous delivery: fund-vista push → NAS deploy

Push to `origin/main` deploys itself to the NAS `fundvista` container
within ~10 minutes (build + two smoke gates). Test-gated: only the exact
commit that passed the laminar `fund-vista` job is ever deployed.

## Chain

1. GitHub `push` webhook → `POST https://ci.linuxadm.hu/hooks/github-ci`
   (repo webhook id `685475003`, push only, secret in `/srv/ci/.env`).
2. `hook-github.sh` maps `KissPeter/fund-vista` → queues laminar job
   `fund-vista` with `ref`/`sha`/`branch` (see `/srv/ci`, repo
   `ci-laminar`).
3. `fund-vista` runs the backend pytest suite (+ frontend hermetic lane)
   and posts `ci/laminar` status. Its `.after` queues
   `fund-vista-deploy` with the same `sha` — but only when
   `RESULT=success` **and** `branch=main`. PR branches and red mains
   never deploy.
4. `fund-vista-deploy` (same `fundvista` laminar context, single
   executor — deploys serialize against each other and against test
   runs) runs `ops/deploy-nas.sh <sha>` from this repo and posts
   `ci/laminar-deploy` status, so the deploy never overwrites the test
   gate check.

## What a deploy does (`ops/deploy-nas.sh`)

1. Disk gate (≥2G free on `/`), fetch + checkout the exact `<sha>`
   (refuses a dirty tree; `main` mode fast-forwards ff-only).
2. Idempotence: if the live container already carries label
   `fundvista.commit=<sha>`, exit after prune.
3. `docker build -t fundvista:nas-<short>` (reuses the tag when the
   image already exists), previous image kept as `fundvista:nas-prev`.
4. One-time `/data` migration: the pre-CD container ran without the
   `fundvista-data` volume — its content is tar-piped into the volume
   once, then every container mounts `-v fundvista-data:/data`.
5. Candidate `fundvista-next` (`WORKERS=1`, internal only, no published
   ports) → smoke: `/healthz` answers, `/v1/health` is 200 with
   `"status":"ok"`. On failure the live container is untouched.
6. Swap: stop/rm `fundvista`, start the new image with the production
   flags (`--network fundvista-net --restart unless-stopped
   -p 127.0.0.1:8100:8100 --cpus=3.0 --memory=4g -v fundvista-data:/data
   --env-file /opt/fund-vista/.env`), then smoke the live container.
   The quota and the `/data` source are env overrides, so the same script
   serves both hosts: `FV_CPUS` / `FV_MEMGB` (defaults 3.0 / 4) and
   `FV_DATA_DIR` (unset → the `fundvista-data` volume; set → a host
   directory bind mount). The Mac CD sets `FV_CPUS=5.0`,
   `FV_MEMGB=4` and `FV_DATA_DIR=/Users/kissp/srv/fund-vista-data`
   (permanent storage on the host disk — see
   `docs/CR-004-permanent-data-storage.md`).
7. Rollback: if the live smoke fails, the previous image is started
   again with the same flags and smoked (90 s). Public traffic falls
   back to FastAPI Cloud via the VPS nginx while this happens
   (see `docs/nas-fundvista.md`), so a double failure is an outage of
   the primary only, never of the public site.
8. Prune: keep newest 3 `fundvista:nas*` images (live + rollback +
   spare) and newest 2 `fund-vista:ci-*` CI images; never force-removes
   an image a container uses (`docker rmi` fails cleanly there).

## Operating

- Watch a deploy: `docker exec laminar-ci laminarc show-running`,
  deploy logs are in the laminar run archive
  (`ci.linuxadm.hu` → `fund-vista-deploy` → run).
- Manual deploy of the tip: `sh /opt/fund-vista/ops/deploy-nas.sh`
  (on the NAS host; refuses dirty/diverged trees).
- Manual deploy of a sha: `sh /opt/fund-vista/ops/deploy-nas.sh <sha>`.
- Retry/re-run a deploy: `docker exec laminar-ci laminarc queue
  fund-vista-deploy sha=<full-sha> branch=main ref=refs/heads/main`.
- Disable CD (keep manual deploys): remove the `.after` queue line in
  `/srv/ci/cfg/jobs/fund-vista.after` (or delete the GitHub webhook);
  `ops/deploy-nas.sh` keeps working standalone.
- Verify only: `sh /opt/fund-vista/ops/deploy-nas.sh --verify`.
- Rollback by hand: `docker stop/rm fundvista` + `docker run` with the
  `fundvista:nas-prev` (or older `nas-<short>`) tag and the §6 flags,
  then `--verify`. No data migration is involved (content-addressed
  store + HMAC tokens), so rollback is just the swap.

## Secrets and paths (NAS, never in git)

- `/opt/fund-vista/.env` (`640 root:fundvista`) — runtime env, read by
  the deploy for `--env-file`. When unreadable the deploy inherits the
  live container's inspected env instead (and refuses without
  `PENPIXEL_HMAC_SECRET`).
- `/opt/fund-vista` — prod checkout; CD leaves it on the deployed sha
  (detached HEAD), manual tip-deploys check `main` back out first.
- Image tags live in the local docker store only: `fundvista:nas`
  (rolling = live), `fundvista:nas-<short>` (per deploy),
  `fundvista:nas-prev` (rollback spare).
