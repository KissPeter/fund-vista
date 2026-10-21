# CR-005 — the Mac is the only backend (drop the NAS)

- Status: done
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

## Transition note (why the first deploy after a rename still uses the old name)

The CD runner copies the deploy script out of the checkout **before** the
deploy syncs that checkout to the pushed sha — deliberately, so a deploy
cannot rewrite the script it is running. The consequence: the first deploy
after a rename runs the *pre-rename* script (the copy is taken from the
checkout of the previous sha), and only the following deploy picks up the
new name. The rename therefore lands over two deploys:

1. push the rename → the deploy runs the old script (old image tag, old
   data layout — both still correct for that moment);
2. the checkout now holds the new script → the next deploy uses it.

The runner prefers the new file name and falls back to the old one, so
neither step can fail on a missing script.

The runner is a **long-lived process**: it keeps the hook code it was
started with, so an edit to `~/srv/ci/hooks/hook-github.sh` only reaches
the runner after it is restarted (kill the pid in
`~/srv/ci/cd/lock/pid`, remove the stale `running` dir). The same is
true for a rename of the script the runner copies — the first deploy after
the rename can otherwise fail with `No such file or directory` while the
live container stays untouched (the deploy fails before it touches it).

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
- [x] The live container runs the new image tag (`fundvista:mac-<sha>`),
      keeps the host-directory `/data`, and passes both smokes; the old
      `fundvista:nas*` images are pruned.
- [x] The obsolete laminar-ui reverse tunnel (Mac side) removed, and the
      VPS side dropped with it: `ci.linuxadm.hu` keeps only the webhook
      intake (`/` answers 404 with a pointer to `/hooks/github-ci`).
- [x] The VPS vhosts no longer name the NAS (`pen-pixel.hu`,
      `ci.linuxadm.hu`, `openchamber.linuxadm.hu`), and the documented
      fallback exclusion is implemented: `location ~ ^/v1/(traffic|jobs)(/|$)`
      keeps the upstream error visible instead of falling back to the cloud
      (which has no slices and no job state).
- [x] Backend suite green (677 passed, 1 skipped; the known pre-existing
      `test_penplot_drawing` failure).
- [x] Docs: this file, `docs/index.md`.

## Follow-ups

- The FastAPI Cloud fallback still runs the old code; it is a fallback,
  not a peer.
- The vhost backups from this change are on the VPS:
  `/root/{ci.linuxadm.hu,pen-pixel.hu,openchamber.linuxadm.hu}.bak-20261021`.
