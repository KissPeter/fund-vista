# CR-009 — Mac disk-space guard + active VM recovery (issue #111)

- Status: done
- Created: 2026-10-26
- Last updated: 2026-11-21
- Type: change request
- Issue: https://github.com/KissPeter/fund-vista/issues/111
- Decided: a shell guard on the Mac host (launchd, hourly) alerts when the
  host volume passes the free-space floor, prunes only regenerable caches and
  clean aged worktrees, and — when the breach persists — restarts Colima and
  retries Docker prune + `fstrim`. A breach MUST recover automatically; the
  read-only-docker failure keeps a manual runbook for when automation is
  unavailable.

## Why

Observed on 2026-10-22 while running issue #94: the host volume hit 100%
(282 MiB free on 228 GiB) and the Colima VM's ext4 journal aborted:

```
EXT4-fs error (device vda1): ext4_journal_check_start: Detected aborted journal
EXT4-fs (vda1): Remounting filesystem read-only
```

Afterwards every `docker run` / `docker build` / `docker image prune`
failed with `write /var/lib/docker/...: read-only file system`; mount
options showed `emergency_ro`, so `mount -o remount,rw` was refused
("Invalid argument"). The only recovery that worked was a VM restart
(see the runbook below). The contributors were host caches
(`~/Library/Caches/ms-playwright` ~1.4 GB, `~/Library/Caches/rancher-desktop-updater`
~1.2 GB) and ~20 old `pen-pixel-shop-*` / `fund-vista.worktrees/*`
checkouts — the VM disk (100 GiB) was never the constraint.

On 2026-11-21 the same class of failure returned with a different shape: the
host volume reached `0 GiB` free from 06:00 to 11:00, `colima status` began
failing with `error retrieving current runtime: empty value`, and the guard's
`colima status` gate skipped the only effective cleanup. The container's TLS
CA bundle was unreadable, so `POST /v1/citymap/render` returned 500. Manual
cache deletion, `colima stop` / `colima start` and `fstrim` restored it.

## What was built

- `ops/mac-disk-guard.sh` — the guard. Measures the host volume (`GUARD_MOUNT`,
  default `/`); below the floor (`GUARD_MIN_FREE_GB`, default 5 GiB, or ≥ 95%
  used) it logs via `logger -t disk-guard` and fires a macOS notification.
  With `PRUNE=1` it deletes regenerable caches (uv, npm, Homebrew, Colima,
  JetBrains, browsers, updaters) and agent worktrees under `WORKTREE_DIR`
  that are older than `WORKTREE_AGE_DAYS` (14) **and** have a clean
  `git status`, then prunes Docker images/build cache and runs `fstrim`
  inside Colima. The VM prune MUST NOT be gated on `colima status`.
  With `PRUNE=1 RECOVER=1` (the launchd default), a floor still breached
  after cleanup restarts Colima once per `RECOVERY_RETRY_HOURS` (default 6)
  and retries the VM cleanup. Dirty worktrees are skipped; the deployed
  checkout (`~/srv/fund-vista`), the `/data` store, running containers and
  git state are never touched. `DRY_RUN=1` only prints.
  Exit 0 healthy, 1 floor breached (even if recovery succeeded), 2 cannot
  stat.
- `ops/mac-disk-guard.plist` — the launchd agent template
  (`hu.linuxadm.disk-guard`, `StartInterval` 3600 so all 24 hours are covered;
  install steps in the file header comment).
- `ops/tests/test_mac_disk_guard.sh` — hermetic stubs for `df`, `colima`,
  `docker`, caches and state; covers healthy, prune-only, broken-VM recovery,
  backoff and dry-run paths.
- Runbook: the next section, also linked from `docs/mac-fundvista.md`.

## Runbook — "docker: ... read-only file system"

1. Confirm the host volume, not the VM disk, is the constraint:
   `df -h /` on the Mac (the host), `colima ssh -- df -h /var/lib/docker`.
   If the host volume is full, run the guard with cleanup enabled:
   `PRUNE=1 sh ops/mac-disk-guard.sh`. The normal launchd agent already
   does this hourly; run it by hand only when you need the result now.
2. If the VM did not come back cleanly, restart Colima (the write lock
   survives `mount -o remount,rw` — do not try that):

   ```sh
   colima stop
   colima start
   colima ssh -- sudo fstrim -av
   ```

   On boot the journal replays and `/var/lib/docker` remounts rw; `fstrim`
   hands the freed blocks back to the host's sparse disk image.
3. The `unless-stopped` containers (`fundvista`, `fundvista-redis`,
   `ci-webhook`) come back on their own; after they are up, check
   `docker ps`, `curl -fsS http://127.0.0.1:8100/healthz` and
   `curl -fsS -X POST https://pen-pixel.hu/v1/citymap/render` with a
   small bbox payload.
4. If a journal-aborted state repeats with disk space already free,
   look at Colima/lima updates before suspecting the volume.

## Definition of Done

- [x] The guard script + launchd template exist in `ops/` and are documented.
- [x] The prune path is conservative: clean, aged worktrees and regenerable
      caches only; nothing of the deployment, the data or running containers.
- [x] The read-only-docker recovery runbook is written and linked from
      `docs/mac-fundvista.md`.
- [x] `ops/tests/test_mac_disk_guard.sh` covers the guard's branches
      hermetically; `shellcheck -s sh` and `sh -n` are clean.

## Phase 2 — active recovery (2026-11-21)

On 2026-11-21 the host volume reached `0 GiB` free from 06:00 through 11:00.
The guard detected every breach, but `colima status` was already failing with
`error retrieving current runtime: empty value`, so the Colima `docker prune`
and `fstrim` branch was skipped. The other prune targets no longer existed or
were younger than the 14-day age floor. The app only recovered after a manual
`colima stop` / `colima start` and `fstrim`.

The guard MUST therefore recover, not just alert:

- [x] Hourly schedule covers every hour, including 23:00–05:00.
- [x] The Docker/VM prune and `fstrim` MUST run without a `colima status`
      gate; a failed status check MUST be treated as a reason to recover,
      not a reason to skip cleanup.
- [x] When the floor is still breached after cache and VM cleanup, the guard
      MUST restart Colima once per `RECOVERY_RETRY_HOURS` window, then retry
      VM cleanup.
- [x] The cache prune MUST include the regenerable caches that actually
      grew: uv, npm, Homebrew, Colima, JetBrains, browser and updater caches.
- [x] A hermetic shell test MUST cover healthy, prune-only, broken-VM recovery,
      recent-recovery backoff and dry-run paths.
- [x] The installed launchd agent on this Mac MUST be refreshed from the
      repository plist, and the public `/v1/citymap/render` check MUST be
      green after the merge.
