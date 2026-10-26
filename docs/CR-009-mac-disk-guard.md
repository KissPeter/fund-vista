# CR-009 — Mac disk-space guard + the read-only-docker recovery runbook (issue #111)

- Status: done
- Created: 2026-10-26
- Last updated: 2026-10-26
- Type: change request
- Issue: https://github.com/KissPeter/fund-vista/issues/111
- Decided: a shell guard on the Mac host (launchd, 2× daily) alerts when the
  host volume passes the free-space floor and, in prune mode, deletes only
  the two regenerable contributors; the read-only-docker failure gets its
  own recovery runbook instead of tribal knowledge.

## Why

Observed on 2026-10-22 while running issue #94: the host volume hit 100%
(282 MiB free on 228 GiB) and the Rancher Desktop VM's ext4 journal aborted:

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

## What was built

- `ops/mac-disk-guard.sh` — the guard. Measures the host volume (`GUARD_MOUNT`,
  default `/`); below the floor (`GUARD_MIN_FREE_GB`, default 5 GiB, or ≥ 95%
  used) it logs via `logger -t disk-guard` and fires a macOS notification.
  With `PRUNE=1` it also deletes `~/Library/Caches/ms-playwright`,
  `~/Library/Caches/rancher-desktop-updater`, and agent worktrees under
  `~/PycharmProjects/fund-vista.worktrees` that are older than
  `WORKTREE_AGE_DAYS` (14) **and** have a clean `git status` — dirty ones
  are skipped, the deployed checkout (`~/srv/fund-vista`), the `/data`
  store and git state are never touched. `DRY_RUN=1` only prints.
  Exit 0 healthy, 1 floor breached, 2 cannot stat.
- `ops/mac-disk-guard.plist` — the launchd agent template
  (`hu.linuxadm.disk-guard`, 06:00 and 18:00; install steps in the file
  header comment).
- Runbook: the next section, also linked from `docs/mac-fundvista.md`.

## Runbook — "docker: ... read-only file system"

1. Confirm the host volume, not the VM disk, is the constraint:
   `df -h /` on the Mac (the host), `docker system df` inside the VM.
   If the host volume is full: free space first with the guard —
   `PRUNE=1 sh ops/mac-disk-guard.sh` — or by hand.
2. Recover the VM (write lock survives `mount -o remount,rw` — do not try):

   ```sh
   rdctl shutdown
   rdctl start
   ```

   On boot the journal replays and `/var/lib/docker` remounts rw.
3. The `unless-stopped` containers (`fundvista`, `fundvista-redis`,
   `ci-webhook`) come back on their own; after they are up, check
   `docker ps` and `curl -fsS http://127.0.0.1:8100/healthz`.
4. If a journal-aborted state repeats with disk space already free,
   look at Rancher Desktop/lima updates before suspecting the volume.

## Definition of Done

- [x] The guard script + launchd template exist in `ops/` and are documented.
- [x] The prune path is conservative: clean, aged worktrees and two
      regenerable caches only; nothing of the deployment or the data.
- [x] The read-only-docker recovery runbook is written and linked from
      `docs/mac-fundvista.md`.
- [x] Backend suite still green (`runtests.sh`) — the change is ops/docs
      only, so this is a regression check, not coverage for the script
      itself; the script is exercised on the Mac host directly.
