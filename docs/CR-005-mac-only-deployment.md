# CR-005 — the Mac is the only backend (drop the NAS)

- Status: in progress
- Created: 2026-10-21
- Last updated: 2026-10-21
- Type: change request
- Decided: the NAS is decommissioned. The repo stops describing it as the
  deploy target: the deploy script, the runbooks, the operator skill and
  the code comments all name the Mac, and the NAS-only branches (named
  volume, separate quota defaults) are removed.

## Motivation

CR-003 moved the deploy target to the Mac and CR-004 moved the store to
the Mac's disk, but the repo still carried the NAS framing everywhere:

- `ops/deploy-nas.sh` was the deploy script (used by the Mac CD, the NAS
  CI, and every doc pointing at it).
- `docs/nas-cd.md` / `docs/nas-fundvista.md` were the runbooks — and
  `docs/nas-cd.md` still described the **laminar** chain that was retired
  on the Mac, so it described neither host correctly.
- `.opencode/skills/deploy-nas/` was the operator skill: SSH to
  `192.168.0.240`, `Dockerfile.nas`, `fundvista:nas`, a systemd tunnel.
- The script still supported the NAS-only shapes: the `fundvista-data`
  named volume as the default `/data`, `FV_CPUS=3.0` as the default
  quota, `fundvista:nas-*` image tags, and a CI-image prune step for a
  CI stack that no longer exists here.

The NAS host itself is gone: SSH times out, and the public path
(`pen-pixel.hu` → VPS nginx → reverse tunnel → `:8100`) resolves to the
Mac. Nothing falls back to it any more — FastAPI Cloud is the only
fallback.

## Scope

- `ops/deploy-nas.sh` → `ops/deploy-mac.sh`:
  - `/data` is always the host directory (`FV_DATA_DIR`, default
    `/Users/kissp/srv/fund-vista-data`); the named-volume branch and
    `DATA_VOL` are gone.
  - Image tags `fundvista:mac-<short>` / `fundvista:mac` /
    `fundvista:mac-prev`; the CI-image prune step is gone.
  - Defaults are the Mac's: `FV_CPUS=5.0`, `FV_MEMGB=4`.
  - `migrate_data()` keeps its safety net, retargeted at the directory.
- `docs/nas-cd.md` → `docs/mac-cd.md`: the real chain (GitHub push webhook
  → VPS nginx → autossh tunnel → `ci-webhook` → `hook-github.sh` → runner
  → `ops/deploy-mac.sh`), the deploy steps, the operating notes and the
  Mac's secrets/paths.
- `docs/nas-fundvista.md` → `docs/mac-fundvista.md`: the Mac topology
  (Rancher Desktop VM, containers, host-directory store, redis cache,
  launchd tunnels), verification, failover drill, gotchas.
- `.opencode/skills/deploy-nas/` → `.opencode/skills/deploy-mac/`: the
  operator skill (id `deploy-mac`) rewritten for the Mac.
- Code comments: the backend's "NAS-only" / "NAS data volume" notes now
  say Mac; the `tls.py` comment about the 2026-10-20 incident keeps its
  history but drops the host name.
- `Dockerfile`: the run example is the Mac's (5 CPUs, host-directory
  `/data`, `fundvista:mac`).
- The CD receiver (`~/srv/ci`) follows the rename: the runner prefers
  `ops/deploy-mac.sh` and falls back to the old name for one deploy, so
  the rename can never break a running CD.

## Explicitly unchanged

- The deploy algorithm (sync → build → candidate smoke → swap → live
  smoke/rollback → prune) and its gates.
- The store layout (`/data/traffic/day/<day>/<ICAO>.json.gz`,
  `/data/parsed`, `/data/results`, `/data/images`) and the git-carried
  slices (`backend/traffic/slices/`).
- The VPS nginx vhost: it already serves the Mac (`proxy_pass
  http://127.0.0.1:8100`, `X-Penplot-Upstream: mac`) and keeps the
  FastAPI Cloud fallback for 502/504.
- The FastAPI Cloud fallback, and the `PENPIXEL_HMAC_SECRET` shared with
  it (HMAC verify is cross-backend).

## DoD

- [x] `ops/deploy-mac.sh` deploys from the Mac with the host-directory
      store, Mac image tags and Mac quota defaults; `sh -n` clean.
- [x] `docs/mac-cd.md` and `docs/mac-fundvista.md` describe the Mac as
      the only backend; no NAS runbook remains.
- [x] The operator skill is `deploy-mac` (id + description) and matches
      the runbooks.
- [x] Backend code comments and the Dockerfile example name the Mac.
- [x] The CD receiver follows the rename without a gap (old name
      fallback) and a real deploy lands on the new script.
- [x] The live container runs the new image tag, keeps the host-directory
      `/data`, and passes both smokes.
- [x] Old `fundvista:nas*` images pruned; the obsolete laminar-ui
      reverse tunnel removed.
- [x] Backend suite green (677 passed, 1 skipped; the known pre-existing
      `test_penplot_drawing` failure).
- [x] Docs: this file, `docs/index.md`.

## Follow-ups

- The nginx vhost still carries two "NAS" comments and the
  `@fundvista_cloud` fallback would answer `/v1/traffic/*` with a
  `job_not_found` if the Mac ever answers 502/504 on that path — the
  documented exclusion is not implemented in the vhost.
- The FastAPI Cloud fallback still runs the old code; it is a fallback,
  not a peer.
