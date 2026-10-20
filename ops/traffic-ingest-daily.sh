#!/bin/sh
# Daily traffic ingest → git push (cron/launchd; NAS: /etc/cron.d/traffic-ingest,
# Mac: ~/Library/LaunchAgents/hu.linuxadm.traffic-ingest.plist).
#
# Flow: for each ICAO, POST a traffic_ingest job for yesterday (UTC) to the
# local backend, poll it to terminal state, then export the new slice files
# from the app container into this repo and push them to origin/main — so the
# slices live in git (HU-only scope: ~2 MB per airport-day) and every deploy
# carries them. Idempotent: already-ingested days return cache_hit instantly
# and produce no commit; a failed push is retried by the next daily run.
#
# Env overrides: REPO (default /opt/fund-vista), API (default
# http://127.0.0.1:8100), TRAFFIC_ICAOS (space-separated, default "LHBP" —
# one archive pass per airport, each ~3.5 GB download, so keep this short).
# Logs to stdout (the scheduler redirects to a log file).
set -u

REPO="${REPO:-/opt/fund-vista}"
API="${API:-http://127.0.0.1:8100}"
ICAOS="${TRAFFIC_ICAOS:-LHBP}"
CONTAINER="${CONTAINER:-fundvista}"
SLICES="$REPO/backend/traffic/slices"

# Yesterday (UTC) in GNU and BSD/macOS date flavors.
if date -u -d yesterday +%F >/dev/null 2>&1; then
  YESTERDAY="$(date -u -d yesterday +%F)"
else
  YESTERDAY="$(date -u -v-1d +%F)"
fi

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }

post_job() { # date -> prints job_id (one archive pass for every ICAO)
  icaos_json=$(printf '"%s",' $ICAOS | sed 's/,$//')
  curl -s -m 30 -X POST "$API/v1/jobs" -H 'content-type: application/json' \
    -d "{\"type\":\"traffic_ingest\",\"request\":{\"icaos\":[$icaos_json],\"date\":\"$1\"},\"cancel_previous\":false}" |
    python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
print(d.get("job_id", ""))'
}

post_job_one() { # icao date -> prints job_id (legacy single-airport shape)
  curl -s -m 30 -X POST "$API/v1/jobs" -H 'content-type: application/json' \
    -d "{\"type\":\"traffic_ingest\",\"request\":{\"icao\":\"$1\",\"date\":\"$2\"},\"cancel_previous\":false}" |
    python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
print(d.get("job_id", ""))'
}

wait_job() { # job_id -> 0 done, 1 failed/cancelled, 2 timeout (6 h)
  i=0
  while [ "$i" -lt 360 ]; do
    st="$(curl -s -m 30 "$API/v1/jobs/$1" |
      python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
    case "$st" in
      done) log "job $1 done"; return 0 ;;
      failed | cancelled) log "job $1 $st"; return 1 ;;
    esac
    sleep 60
    i=$((i + 1))
  done
  log "job $1 poll timeout"
  return 2
}

FAILED=0
JOB="$(post_job "$YESTERDAY" || true)"
if [ -n "$JOB" ]; then
  # One archive pass serves the whole list (the API accepts icaos).
  log "ingest [$ICAOS] $YESTERDAY as job $JOB"
  wait_job "$JOB" || FAILED=1
else
  # Older app without the icaos shape: one job per airport (one pass each).
  log "multi-airport enqueue unavailable — per-airport fallback"
  for ICAO in $ICAOS; do
    log "ingest $ICAO $YESTERDAY"
    JOB="$(post_job_one "$ICAO" "$YESTERDAY")"
    case "$JOB" in
      [0-9a-f][0-9a-f]* | [0-9A-F][0-9A-F]*) log "job $JOB for $ICAO" ;;
      *) log "POST failed for $ICAO (no job_id)"; FAILED=1; continue ;;
    esac
    wait_job "$JOB" || FAILED=1
  done
fi
[ "$FAILED" -eq 0 ] || { log "ingest failures — slices not pushed"; exit 1; }

# Export the store out of the container into the repo checkout.
if ! docker exec "$CONTAINER" test -d /data/traffic/day; then
  log "no /data/traffic/day in $CONTAINER — nothing to export"
  exit 0
fi
mkdir -p "$SLICES/day" "$SLICES/pass"
docker cp "$CONTAINER:/data/traffic/day/." "$SLICES/day/" || exit 1
if docker exec "$CONTAINER" test -d /data/traffic/pass; then
  docker cp "$CONTAINER:/data/traffic/pass/." "$SLICES/pass/" || exit 1
fi

cd "$REPO" || exit 1
# CD deploys leave the prod checkout detached at the deployed sha; the git
# export needs a branch. The tree is clean here (deploy-nas.sh refuses dirty
# trees), so checking main out is safe.
if [ -z "$(git symbolic-ref -q HEAD)" ]; then
  git checkout -q main || { log "cannot check out main (detached HEAD)"; exit 1; }
fi
git pull --rebase origin main || { log "git pull failed"; exit 1; }
git add backend/traffic/slices || exit 1
if git diff --cached --quiet; then
  log "no new slices for $YESTERDAY"
  exit 0
fi
git commit -m "data(traffic): daily slices $YESTERDAY ($ICAOS)" || exit 1
if ! git push origin main; then
  git pull --rebase origin main && git push origin main || { log "git push failed"; exit 1; }
fi
log "pushed slices for $YESTERDAY"
