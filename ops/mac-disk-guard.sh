#!/bin/sh
# Disk-space guard for the Mac (issue #111, CR-009 phase 2).
#
# The host volume hitting 100% once aborted the Colima VM's ext4 journal
# ("Detected aborted journal"), remounting /var/lib/docker read-only and
# breaking both docker calls and the TLS CA bundle reads inside fundvista.
# A guard that only alerts cannot fix that: once `colima status` is unhealthy
# the docker prune/fstrim path is skipped, so the VM disk image keeps growing
# on the host. This script MUST recover the VM as well as alert.
#
# Modes:
#   alert (default)  Host volume free space below the floor → log via
#                    logger(1) + a macOS notification, exit 1.
#   prune (PRUNE=1)  Additionally delete regenerable caches and old clean
#                    worktrees, prune unused Docker images/build cache and
#                    fstrim the Colima disk image.
#   recover (RECOVER=1, with PRUNE=1)
#                    When the floor is still breached after cleanup, restart
#                    Colima (journal replay), retry the Docker prune and
#                    fstrim, and log the outcome. A restart MUST be attempted
#                    at most once per RECOVERY_RETRY_HOURS to avoid bouncing
#                    the production VM on every hourly run.
#
# The guard MUST NOT touch /opt/fund-vista, the /data store, git state,
# running containers or anything newer than the worktree age threshold.
# All paths are env-overridable so the hermetic test can point them at a
# temporary directory.
#
# Intended as a launchd agent (hourly, all 24 hours); see
# ops/mac-disk-guard.plist and docs/CR-009-mac-disk-guard.md.
#
# Env overrides: GUARD_MOUNT (default /), GUARD_MIN_FREE_GB (default 5),
# CACHE_ROOT (default $HOME), WORKTREE_DIR
# (default $HOME/PycharmProjects/fund-vista.worktrees), WORKTREE_AGE_DAYS
# (default 14), PRUNE (default 0, 1 = also prune), RECOVER (default 1,
# 1 = restart Colima on a persistent breach), RECOVERY_RETRY_HOURS (default 6),
# STATE_DIR (default $HOME/.local/state/fund-vista), DRY_RUN (default 0,
# 1 = show what would happen without deleting or restarting).
set -u

MOUNT="${GUARD_MOUNT:-/}"
MIN_FREE_GB="${GUARD_MIN_FREE_GB:-5}"
CACHE_ROOT="${CACHE_ROOT:-$HOME}"
WORKTREE_DIR="${WORKTREE_DIR:-$HOME/PycharmProjects/fund-vista.worktrees}"
WORKTREE_AGE_DAYS="${WORKTREE_AGE_DAYS:-14}"
PRUNE="${PRUNE:-0}"
RECOVER="${RECOVER:-1}"
RECOVERY_RETRY_HOURS="${RECOVERY_RETRY_HOURS:-6}"
STATE_DIR="${STATE_DIR:-$HOME/.local/state/fund-vista}"
RECOVERY_STAMP="$STATE_DIR/disk-guard-last-recovery"
DRY_RUN="${DRY_RUN:-0}"

for n in "$MIN_FREE_GB" "$WORKTREE_AGE_DAYS" "$RECOVERY_RETRY_HOURS"; do
    case "$n" in
        ''|*[!0-9]* ) echo "disk-guard: numeric env value expected, got '$n'" >&2; exit 2;;
    esac
done
RECOVERY_WINDOW_S=$((RECOVERY_RETRY_HOURS * 3600))

read_free_kib() {
    df -P -k "$MOUNT" 2>/dev/null | awk 'NR==2 {print $4}'
}

read_used_pct() {
    df -P -k "$MOUNT" 2>/dev/null | awk 'NR==2 {sub("%", "", $5); print $5}'
}

# 0 = breached, 1 = healthy, 2 = cannot stat.
floor_breached() {
    free_kib=$(read_free_kib)
    used_pct=$(read_used_pct)
    [ -n "$free_kib" ] && [ -n "$used_pct" ] || return 2
    case "$free_kib" in
        ''|*[!0-9]* ) return 2;;
    esac
    case "$used_pct" in
        ''|*[!0-9]* ) return 2;;
    esac
    free_gb=$((free_kib / 1024 / 1024))
    [ "$free_gb" -lt "$MIN_FREE_GB" ] || [ "$used_pct" -ge 95 ]
}

notify() {
    # $1 = message sent to both syslog and the macOS notification centre.
    logger -t disk-guard -p user.err "$1" 2>/dev/null || true
    osascript -e "display notification \"$1\" with title \"fund-vista disk guard\" sound name \"Basso\"" \
        >/dev/null 2>&1 || true
}

prune_one() {
    # prune_one <path>: delete a regenerable path (or show it on a dry run).
    [ -e "$1" ] || return 0
    if [ "$DRY_RUN" = "1" ]; then
        echo "PRUNE (dry-run): $1"
    else
        echo "PRUNE: $1"
        # Some npm caches carry the immutable flag; clear it before rm.
        if command -v chflags >/dev/null 2>&1; then
            chflags -R nouchg "$1" 2>/dev/null || true
        fi
        rm -rf "$1" 2>/dev/null || echo "WARN: could not prune $1" >&2
    fi
}

prune_caches() {
    # Only regenerable caches. A cache MAY be recreated; the /data store and
    # the checkout MUST NOT be pruned.
    prune_one "$CACHE_ROOT/Library/Caches/ms-playwright"
    prune_one "$CACHE_ROOT/Library/Caches/rancher-desktop-updater"
    prune_one "$CACHE_ROOT/Library/Caches/rancher-desktop"
    prune_one "$CACHE_ROOT/Library/Caches/colima"
    prune_one "$CACHE_ROOT/Library/Caches/Homebrew"
    prune_one "$CACHE_ROOT/Library/Caches/Google"
    prune_one "$CACHE_ROOT/Library/Caches/@openchamberelectron-updater"
    prune_one "$CACHE_ROOT/Library/Caches/JetBrains"
    prune_one "$CACHE_ROOT/Library/Caches/node-gyp"
    prune_one "$CACHE_ROOT/.cache/uv"
    prune_one "$CACHE_ROOT/.cache/pkg"
    prune_one "$CACHE_ROOT/.cache/prisma"
    prune_one "$CACHE_ROOT/.npm/_cacache"
}

prune_worktrees() {
    [ -d "$WORKTREE_DIR" ] || return 0
    for wt in "$WORKTREE_DIR"/*/; do
        [ -d "$wt" ] || continue
        wt="${wt%/}"
        mtime=$(stat -f %m "$wt" 2>/dev/null || echo 0)
        case "$mtime" in
            ''|*[!0-9]* ) mtime=0;;
        esac
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
}

vm_prune() {
    # This MUST NOT be gated on `colima status`. A broken runtime is exactly
    # when the prune/fstrim is needed, and `colima ssh` MAY still work.
    if ! command -v colima >/dev/null 2>&1; then
        echo "SKIP: colima not installed"
        return 0
    fi
    if [ "$DRY_RUN" = "1" ]; then
        echo "PRUNE (dry-run): docker image/builder prune in colima"
        colima ssh -- docker system df 2>&1 || true
        return 0
    fi
    echo "PRUNE: docker image + builder cache in colima"
    colima ssh -- docker image prune -af --filter "until=24h" 2>&1 || true
    colima ssh -- docker builder prune -af 2>&1 || true
    echo "TRIM: colima VM disk"
    colima ssh -- sudo fstrim -av 2>&1 || true
}

recovery_due() {
    [ "$RECOVER" = "1" ] || return 1
    command -v colima >/dev/null 2>&1 || return 1
    [ "$DRY_RUN" = "1" ] && return 0
    now=$(date +%s)
    last=0
    if [ -r "$RECOVERY_STAMP" ]; then
        last=$(cat "$RECOVERY_STAMP" 2>/dev/null || echo 0)
    fi
    case "$last" in
        ''|*[!0-9]* ) last=0;;
    esac
    elapsed=$((now - last))
    [ "$elapsed" -ge "$RECOVERY_WINDOW_S" ]
}

recover_vm() {
    echo "RECOVER: restarting colima (journal replay)"
    if [ "$DRY_RUN" = "1" ]; then
        echo "RECOVER (dry-run): colima stop + colima start"
        return 0
    fi
    # Stamp before the restart: if the script dies mid-recovery the next run
    # MUST NOT immediately bounce the VM again.
    mkdir -p "$STATE_DIR" 2>/dev/null || echo "WARN: cannot create $STATE_DIR" >&2
    date +%s > "$RECOVERY_STAMP" 2>/dev/null || echo "WARN: cannot stamp $RECOVERY_STAMP" >&2
    colima stop 2>&1 || true
    colima start 2>&1 || true
    if colima status >/dev/null 2>&1 || docker info >/dev/null 2>&1; then
        echo "RECOVER: colima is up"
        return 0
    fi
    echo "RECOVER: colima did not come up" >&2
    return 1
}

free_kib=$(read_free_kib)
used_pct=$(read_used_pct)
if [ -z "$free_kib" ] || [ -z "$used_pct" ]; then
    echo "disk-guard: cannot stat $MOUNT" >&2
    exit 2
fi
free_gb=$((free_kib / 1024 / 1024))
echo "$(date '+%F %T') mount=$MOUNT used=${used_pct}% free=${free_gb}GiB (floor ${MIN_FREE_GB}GiB)"

case "$free_kib" in
    ''|*[!0-9]* ) echo "disk-guard: cannot stat $MOUNT" >&2; exit 2;;
esac
case "$used_pct" in
    ''|*[!0-9]* ) echo "disk-guard: cannot stat $MOUNT" >&2; exit 2;;
esac
breached=0
if [ "$free_gb" -lt "$MIN_FREE_GB" ] || [ "$used_pct" -ge 95 ]; then
    breached=1
fi

if [ "$breached" -ne 1 ]; then
    exit 0
fi

notify "host volume $MOUNT low: ${free_gb}GiB free (${used_pct}% used)"

if [ "$PRUNE" = "1" ]; then
    prune_caches
    # No status gate: a broken VM is the reason to prune, not to skip.
    vm_prune
    prune_worktrees

    if floor_breached; then
        if recovery_due; then
            if recover_vm; then
                vm_prune
            fi
        else
            echo "RECOVER: skipped (disabled, no colima, or attempted within ${RECOVERY_RETRY_HOURS}h)"
        fi
    else
        echo "RECOVER: not needed — below floor after prune"
    fi
else
    echo "PRUNE: disabled (set PRUNE=1 to clean and recover)"
fi

free_kib=$(read_free_kib)
used_pct=$(read_used_pct)
case "$free_kib" in
    ''|*[!0-9]* ) free_kib=0;;
esac
case "$used_pct" in
    ''|*[!0-9]* ) used_pct=0;;
esac
free_gb=$((free_kib / 1024 / 1024))
echo "$(date '+%F %T') final mount=$MOUNT used=${used_pct}% free=${free_gb}GiB"
logger -t disk-guard -p user.warning "prune/recovery run finished on $MOUNT (${free_gb}GiB free)" 2>/dev/null || true

# Keep the historical contract: exit 1 means a floor breach was observed,
# even when recovery has already brought the volume back under the floor.
exit 1
