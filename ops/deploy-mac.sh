#!/bin/sh
# Deploy the fund-vista backend on the Mac (see docs/mac-cd.md).
# Same steps the runbook prescribes: sync -> build -> candidate smoke ->
# swap -> live smoke (rollback on failure) -> prune.
#
# Usage (on the Mac host, or inside the ci-webhook container which mounts
# the host docker.sock AND /opt/fund-vista):
#   ops/deploy-mac.sh              # deploy origin/main tip (pull, ff-only)
#   ops/deploy-mac.sh <sha>        # deploy the exact tested commit (CD path)
#   ops/deploy-mac.sh --verify     # read-only: smoke-test the live container
#   ops/deploy-mac.sh --prune      # image GC only (never touches the live container)
#
# Env overrides: FV_REPO (default /opt/fund-vista), SMOKE_TIMEOUT (default 150s),
# FV_CPUS/FV_MEMGB (prod container quota, defaults 5.0/4 — the VM has 6 CPUs
# and the airport traffic ingest saturates a smaller quota),
# FV_DATA_DIR (default /Users/kissp/srv/fund-vista-data — the host directory
# the container's /data is bind-mounted from; permanent across a VM rebuild).
# Requires: live env from $FV_REPO/.env (never in git); falls back to the
# live container's inspected env when the file is unreadable (the CD path
# uses the same /opt mount, so this is only a safety net).
set -eu

# The container sees the checkout at /opt/fund-vista; on the host it is
# ~/srv/fund-vista. Pick whichever exists unless FV_REPO is set.
if [ -z "${FV_REPO:-}" ]; then
  if [ -d /opt/fund-vista/.git ]; then
    FV_REPO=/opt/fund-vista
  else
    FV_REPO="$HOME/srv/fund-vista"
  fi
fi
REPO="$FV_REPO"
CONTAINER="fundvista"
CANDIDATE="fundvista-next"
IMAGE="fundvista"
NET="fundvista-net"
# Where /data lives: a host directory on the Mac's own disk, bind-mounted
# into the container. Not a Docker volume — a volume lives inside the
# container engine's disk image and is lost on a VM rebuild (the store did
# not survive one). The path is resolved by the daemon inside the VM; the
# Mac home is shared there over virtiofs at the same absolute path.
DATA_DIR="${FV_DATA_DIR:-/Users/kissp/srv/fund-vista-data}"
ENV_FILE="$REPO/.env"
PORT="8100"
SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-150}"
FV_CPUS="${FV_CPUS:-5.0}"
FV_MEMGB="${FV_MEMGB:-4}"

log() { echo "$(date -u +%FT%TZ) $*"; }
fail() { echo "DEPLOY FAILED: $1" >&2; exit 1; }

diskcheck() {
  # POSIX df only: job scripts run under BusyBox ash in laminar-ci, where
  # GNU `df --output` does not exist (fatal there, silent on Debian).
  FREE_KB=$(df -P -k / | tail -1 | awk '{print $4}')
  case "$FREE_KB" in
    *[!0-9]* | "" ) fail "could not read disk space";;
  esac
  FREE=$((FREE_KB * 1024))
  log "disk free on /: ${FREE} bytes"
  case "$FREE" in
    *[!0-9]* | "" ) fail "could not read disk space";;
  esac
  # One fundvista image is ~500M; build + swap needs headroom for two.
  [ "$FREE" -ge 2147483648 ] \
    || fail "only ${FREE} bytes free on / (<2G) — prune old images first, then retry"
}

# git fetch that works both on the host (SSH key) and in CI (GH_TOKEN).
do_fetch() {
  if git fetch origin --prune 2>/dev/null; then
    return 0
  fi
  [ -n "${GH_TOKEN:-}" ] || fail "git fetch origin failed (no SSH key and no GH_TOKEN)"
  git -c "url.https://x-access-token:${GH_TOKEN}@github.com/.insteadOf=git@github.com:" \
    fetch origin --prune \
    || fail "git fetch origin failed even with GH_TOKEN"
}

# Resolve the target commit. Prints the full sha.
# $1 = "" (tip: ff-only advance main) or an exact sha (CD: tested commit).
sync_tree() {
  want="${1:-}"
  cd "$REPO"
  [ -d .git ] || fail "$REPO is not a git checkout"
  do_fetch
  if [ -n "$want" ]; then
    git cat-file -e "$want^{commit}" 2>/dev/null \
      || fail "unknown commit '$want' (not in origin after fetch)"
    [ -z "$(git status --porcelain)" ] \
      || fail "working tree dirty — commit first, never force it from CD"
    git checkout -q "$want" \
      || fail "checkout $want failed"
  else
    # Back to main first: CD leaves a detached HEAD behind.
    if [ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then
      [ -z "$(git status --porcelain)" ] \
        || fail "working tree dirty — commit first"
      git checkout -q main || fail "checkout main failed"
    fi
    [ -z "$(git status --porcelain)" ] \
      || fail "working tree dirty — commit first"
    git merge --ff-only origin/main -q \
      || fail "main diverged from origin/main — inspect by hand, never force it"
  fi
  git rev-parse HEAD
}

live_image() {
  docker inspect "$CONTAINER" --format '{{.Image}}' 2>/dev/null || true
}

live_commit() {
  docker inspect "$CONTAINER" --format '{{index .Config.Labels "fundvista.commit"}}' 2>/dev/null || true
}

env_args() {
  # Prints docker run env flags: --env-file ... (plus nothing else).
  # Prefers the host env file (source of truth); reuses the live
  # container's env when the file is unreadable.
  if [ -r "$ENV_FILE" ]; then
    echo "--env-file $ENV_FILE"
    return 0
  fi
  [ -n "$(live_image)" ] \
    || fail "no $ENV_FILE and no live $CONTAINER to inherit env from"
  tmp="$(mktemp /tmp/fv-live-env.XXXXXX)"
  docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' > "$tmp"
  grep -q '^PENPIXEL_HMAC_SECRET=' "$tmp" \
    || fail "live env has no PENPIXEL_HMAC_SECRET — refusing to guess secrets"
  echo "--env-file $tmp"
}

# $1 = image tag to build, $2 = commit sha (label)
do_build() {
  tag="$1"; sha="$2"
  logf="$BUILD_LOG_DIR/fv-build-${tag##*:}.log"
  log "== 2. build $tag ($sha) =="
  if docker image inspect "$tag" >/dev/null 2>&1; then
    log "image $tag already built — reusing"
  else
    docker build --label "fundvista.commit=$sha" \
      --build-arg "FUNDVISTA_VERSION=$(echo "$sha" | cut -c1-7)" \
      --build-arg "FUNDVISTA_BUILT=$(date -u +%FT%TZ)" \
      -t "$tag" "$REPO" > "$logf" 2>&1 \
      || fail "build failed — inspect $logf"
    docker image inspect "$tag" >/dev/null 2>&1 \
      || fail "build finished but $tag missing — inspect $logf"
    tail -2 "$logf"
  fi
}

# $1 = container name to probe (DNS on $NET), $2 = image holding python (for the probe)
smoke() {
  name="$1"; img="$2"
  log "smoke: $name (up to ${SMOKE_TIMEOUT}s)"
  waited=0
  while [ "$waited" -lt "$SMOKE_TIMEOUT" ]; do
    st=$(docker inspect "$name" --format '{{.State.Status}}' 2>/dev/null || echo missing)
    case "$st" in
      running) ;;
      *) log "smoke: $name status=$st — waiting"; sleep 5; waited=$((waited + 5)); continue;;
    esac
    # /healthz: workers accept traffic. /v1/health: store writable + disk ok.
    # A tiny python function (backend/tools/http_get.py), not an inline
    # script inside the docker command string (issue #64).
    if docker run --rm --network "$NET" "$img" python -m backend.tools.http_get \
        "http://$name:$PORT/healthz" 2>/dev/null; then
      body=$(docker run --rm --network "$NET" "$img" python -m backend.tools.http_get \
        "http://$name:$PORT/v1/health" 2>/dev/null || true)
      case "$body" in
        *'"status":"ok"'*|*'"status": "ok"'*)
          log "smoke: $name OK ($body)"; return 0;;
        *) log "smoke: $name /v1/health not ok yet: $body";;
      esac
    else
      log "smoke: $name not answering yet (${waited}s)"
    fi
    sleep 5; waited=$((waited + 5))
  done
  log "smoke: $name FAILED after ${SMOKE_TIMEOUT}s"
  docker logs --tail 20 "$name" 2>&1 | tail -20 || true
  return 1
}

migrate_data() {
  # Safety net: if the live container's /data is not the host directory
  # (drift vs docs/mac-fundvista.md), copy its content into the data dir
  # once (best effort — the store is content-addressed and self-heals via
  # re-upload, so a failed copy must never block the deploy).
  mounts=$(docker inspect "$CONTAINER" --format '{{range .Mounts}}{{println .Destination}}{{end}}' 2>/dev/null || true)
  case "$mounts" in
    *"/data"*) log "data: /data already mounted — nothing to migrate"; return 0;;
  esac
  if ! docker exec "$CONTAINER" sh -c 'ls /data 2>/dev/null | head -1 | grep -q .' 2>/dev/null; then
    log "data: live /data empty or unreadable — skipping migration"
    return 0
  fi
  img="$1"
  log "data: migrating live /data into $DATA_DIR"
  # NOTE: stream mode 'r|' — the tar arrives on a non-seekable stdin
  # pipe and plain tarfile.open() dies with "Illegal seek" (seen live).
  if docker cp "$CONTAINER:/data/." - 2>/dev/null \
    | docker run --rm -i -v "$DATA_DIR:/to" "$img" \
      python -m backend.tools.untar_stream /to 2>/dev/null; then
    log "data: migration done"
  else
    log "data: migration FAILED (non-fatal, continuing with empty data dir)"
  fi
}

# Make sure the data source is the real store before the swap.
ensure_data() {
  # The path is resolved by the daemon inside the VM, not by this shell —
  # and docker auto-creates a missing bind source, so a typo would silently
  # land the store in the engine's disk. Probe it daemon-side and require
  # the marker file the operator puts in the real data dir.
  if ! docker run --rm -v "$DATA_DIR:/probe" alpine \
      sh -c 'test -f /probe/.fund-vista-data' >/dev/null 2>&1; then
    fail "FV_DATA_DIR '$DATA_DIR' does not hold a .fund-vista-data marker — refusing to create a stray dir in the engine"
  fi
}

prod_run() {
  # $1 = name, $2 = image, $3 = env-args string, $4 = commit sha, $5 = extra flags
  # shellcheck disable=SC2086
  docker run -d --name "$1" --network "$NET" --restart unless-stopped \
    -p 127.0.0.1:${PORT}:${PORT} --cpus="$FV_CPUS" --memory="${FV_MEMGB}g" \
    -v "$DATA_DIR:/data" $3 --label "fundvista.commit=$4" $5 "$2" >/dev/null
}

do_prune() {
  log "== 7. prune old images (keep newest 3 prod) =="
  docker images --format '{{.Repository}}:{{.Tag}} {{.CreatedAt}}' \
    | grep "^$IMAGE:mac" | sort -k2 -r | tail -n +4 | awk '{print $1}' \
    | xargs -r docker rmi 2>&1 | tail -2 || true
  docker images --format '{{.Repository}}:{{.Tag}}' | grep -E "^$IMAGE:mac" || true
}

if [ "${1:-}" = "--verify" ]; then
  [ -n "$(live_image)" ] || fail "no live $CONTAINER"
  NEW_IMAGE=$(docker inspect "$CONTAINER" --format '{{.Config.Image}}' 2>/dev/null)
  smoke "$CONTAINER" "$NEW_IMAGE"
  log "VERIFY OK"
  exit 0
fi

if [ "${1:-}" = "--prune" ]; then
  do_prune
  exit 0
fi

BUILD_LOG_DIR="${BUILD_LOG_DIR:-/tmp}"

WANT="${1:-}"
case "$WANT" in
  -* ) fail "usage: ops/deploy-mac.sh [<sha>|--verify|--prune]";;
esac

log "== 0. disk space =="
diskcheck

log "== 1. sync $REPO ${WANT:-(origin/main tip)} =="
SHA=$(sync_tree "$WANT")
SHORT=$(git -C "$REPO" rev-parse --short "$SHA")
log "target $SHA ($SHORT) on $(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo detached)"
NEW_IMAGE="$IMAGE:mac-$SHORT"

if [ "$(live_commit)" = "$SHA" ] && [ -n "$(live_image)" ]; then
  log "already live ($SHORT) — nothing to do"
  do_prune
  log "DEPLOY OK: $NEW_IMAGE already live"
  exit 0
fi
if [ -n "$(docker inspect "$CANDIDATE" --format '{{.Id}}' 2>/dev/null || true)" ]; then
  fail "leftover $CANDIDATE exists (previous failed run?) — inspect + 'docker rm -f $CANDIDATE', then retry"
fi

do_build "$NEW_IMAGE" "$SHA"

PREV_ID="$(live_image)"
if [ -n "$PREV_ID" ]; then
  log "previous image $PREV_ID — tagging $IMAGE:mac-prev as rollback spare"
  docker tag "$PREV_ID" "$IMAGE:mac-prev" 2>/dev/null || true
fi

ENV_ARGS=$(env_args)
ensure_data
[ -n "$PREV_ID" ] && migrate_data "$NEW_IMAGE"

log "== 3. candidate $CANDIDATE (WORKERS=1, internal only) =="
# shellcheck disable=SC2086
docker run -d --name "$CANDIDATE" --network "$NET" \
  --cpus=1.0 --memory=1g $ENV_ARGS -e WORKERS=1 \
  --label "fundvista.commit=$SHA" "$NEW_IMAGE" >/dev/null
sleep 5
if ! smoke "$CANDIDATE" "$NEW_IMAGE"; then
  docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
  fail "candidate smoke failed — live $CONTAINER untouched"
fi
docker rm -f "$CANDIDATE" >/dev/null

log "== 4. swap $CONTAINER -> $SHORT =="
if [ -n "$PREV_ID" ]; then
  docker stop "$CONTAINER" >/dev/null 2>&1 || true
  docker rm "$CONTAINER" >/dev/null 2>&1 || true
fi
prod_run "$CONTAINER" "$NEW_IMAGE" "$ENV_ARGS" "$SHA" ""
sleep 5

log "== 5. live smoke =="
if smoke "$CONTAINER" "$NEW_IMAGE"; then
  docker tag "$NEW_IMAGE" "$IMAGE:mac"
  log "tagged $IMAGE:mac -> $SHORT"
else
  log "live smoke FAILED — rolling back"
  docker stop "$CONTAINER" >/dev/null 2>&1 || true
  docker rm "$CONTAINER" >/dev/null 2>&1 || true
  if [ -n "$PREV_ID" ]; then
    prod_run "$CONTAINER" "$PREV_ID" "$ENV_ARGS" "$(live_commit)" "" \
      || fail "rollback container would not start — traffic is on cloud fallback, fix by hand"
    SMOKE_TIMEOUT=90
    if smoke "$CONTAINER" "$PREV_ID"; then
      log "ROLLBACK OK: previous image live again"
    else
      fail "rollback smoke failed too — traffic is on cloud fallback, fix by hand"
    fi
  fi
  fail "new image $SHORT failed live smoke (rolled back)"
fi

do_prune
log "DEPLOY OK: $NEW_IMAGE live"
