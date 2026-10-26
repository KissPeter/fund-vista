#!/bin/sh
# Disk-space guard for the Mac (issue #111): the host volume hitting 100%
# once aborted the Rancher Desktop VM's ext4 journal ("Detected aborted
# journal"), remounting /var/lib/docker read-only and blocking every docker
# call — including the backend test suite that runs inside fundvista-dev.
#
# Two modes:
#   alert (default)  Host volume free space below the floor → log via
#                    logger(1) + a macOS notification, exit 1 (visible to
#                    launchd/mail whatever watches the log).
#   prune (PRUNE=1)  Additionally delete the regenerable space: agent
#                    worktrees older than WORKTREE_AGE_DAYS (clean checkouts
#                    only), and the host caches that caused the original
#                    incident (ms-playwright browsers, the rancher-desktop
#                    updater downloads). Never touches /opt/fund-vista, the
#                    data dir, git state or anything newer than the age
#                    threshold.
#
# Intended as a launchd agent (every 6 h); see ops/mac-disk-guard.plist and
# docs/CR-009-mac-disk-guard.md. Recovery for the read-only-filesystem state
# is in the same doc: `rdctl shutdown` + `rdctl start`.
#
# Env overrides: GUARD_MOUNT (default /), GUARD_MIN_FREE_GB (default 5),
# WORKTREE_DIR (default ~/PycharmProjects/fund-vista.worktrees),
# WORKTREE_AGE_DAYS (default 14), PRUNE (default 0, 1 = also prune),
# DRY_RUN (default 0, 1 = show what prune would delete).
set -u

MOUNT="${GUARD_MOUNT:-/}"
MIN_FREE_GB="${GUARD_MIN_FREE_GB:-5}"
WORKTREE_DIR="${WORKTREE_DIR:-$HOME/PycharmProjects/fund-vista.worktrees}"
WORKTREE_AGE_DAYS="${WORKTREE_AGE_DAYS:-14}"
PRUNE="${PRUNE:-0}"
DRY_RUN="${DRY_RUN:-0}"

# Total/free in KiB, and the used percentage, for the host volume.
total_kib=$(df -k "$MOUNT" | awk 'NR==2 {print $2}')
free_kib=$(df -k "$MOUNT" | awk 'NR==2 {print $4}')
used_pct=$(df -k "$MOUNT" | awk 'NR==2 {sub("%", "", $5); print $5}')
free_gb=$(( free_kib / 1024 / 1024 ))

if [ -z "$total_kib" ] || [ -z "$free_kib" ] || [ "$total_kib" -eq 0 ]; then
    echo "disk-guard: cannot stat $MOUNT" >&2
    exit 2
fi

above_floor=0
if [ "$free_gb" -lt "$MIN_FREE_GB" ] || [ "$used_pct" -ge 95 ]; then
    above_floor=1
fi

echo "$(date '+%F %T') mount=$MOUNT used=${used_pct}% free=${free_gb}GiB (floor ${MIN_FREE_GB}GiB)"

prune_one() {
    # prune_one <path>: delete a regenerable path (or show it on a dry run).
    [ -e "$1" ] || return 0
        if [ "$DRY_RUN" = "1" ]; then
            echo "PRUNE (dry-run): $1"
        else
            echo "PRUNE: $1"
            rm -rf "$1"
        fi
}

if [ "$above_floor" = "1" ]; then
    logger -t disk-guard -p user.err "host volume $MOUNT low: ${free_gb}GiB free (${used_pct}% used)"
    # A notification does not depend on anyone tailing launchd logs.
    osascript -e "display notification \"${free_gb} GiB free (${used_pct}% used)\" with title \"fund-vista disk guard\" sound name \"Basso\"" \
        >/dev/null 2>&1 || true

    # Caches that caused the original incident (regenerable either way).
    if [ "$PRUNE" = "1" ]; then
        prune_one "$HOME/Library/Caches/ms-playwright"
        prune_one "$HOME/Library/Caches/rancher-desktop-updater"
    fi

    # Stale agent worktrees: only clean checkouts older than the threshold.
    if [ "$PRUNE" = "1" ] && [ -d "$WORKTREE_DIR" ]; then
        for wt in "$WORKTREE_DIR"/*/; do
            [ -d "$wt" ] || continue
            wt="${wt%/}"
            mtime=$(stat -f %m "$wt")
            age_days=$(( ( $(date +%s) - mtime ) / 86400 ))
            [ "$age_days" -lt "$WORKTREE_AGE_DAYS" ] && continue
            if [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]; then
                echo "SKIP (dirty): $wt"
                continue
            fi
            # Unregister the git worktree first; the branch stays.
            git -C "$wt" rev-parse --git-dir >/dev/null 2>&1 \
                && git worktree remove --force "$wt" 2>/dev/null \
                || true
            if [ -d "$wt" ]; then  # not a registered worktree anymore
                prune_one "$wt"
            fi
        done
    fi

    if [ "$PRUNE" = "1" ]; then
        # A log of what the run did, for post-mortems.
        logger -t disk-guard -p user.warning "prune run finished on $MOUNT (floor breach)"
    fi

    exit 1
fi

exit 0
