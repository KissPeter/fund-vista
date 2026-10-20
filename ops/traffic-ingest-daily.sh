#!/bin/sh
# Daily traffic ingest → git push (cron/launchd; NAS: /etc/cron.d/traffic-ingest,
# Mac: ~/Library/LaunchAgents/hu.linuxadm.traffic-ingest.plist).
#
# Flow: POST a traffic_ingest job for each recent day (UTC) to the local
# backend — one job covers every ICAO, so one archive pass serves the whole
# list — poll it to terminal state, then export the new slice files from the
# app container into this repo and push them to origin/main — so the slices
# live in git (HU-only scope: ~2 MB per airport-day) and every deploy carries
# them. Idempotent: already-ingested days return cache_hit instantly and
# produce no commit; a failed push is retried by the next daily run.
#
# The window is TRAFFIC_DAYS (default 3) days wide, so a day whose adsb.lol
# archive publishes late is picked up by a later run instead of being lost.
# A day whose archive is not published yet (traffic_not_available) is skipped,
# not treated as a failure — it will be retried while it is still in the
# window.
#
# Env overrides: REPO (default /opt/fund-vista), API (default
# http://127.0.0.1:8100), TRAFFIC_ICAOS (space-separated, default "LHBP"),
# TRAFFIC_DAYS (default 3), CONTAINER (default fundvista).
# Logs to stdout (the scheduler redirects to a log file).
set -u

# A deploy replaces this file in place (the CD checkout is updated by the
# deploy) and a shell reads its script incrementally — a script replaced
# mid-run can abort it with a syntax/unbound-variable error. Work from a
# snapshot copy so the run is immune to that.
if [ -z "${TRAFFIC_INGEST_SNAPSHOT:-}" ]; then
  SNAP="$(mktemp "${TMPDIR:-/tmp}/traffic-ingest.XXXXXX")" || exit 1
  cp "$0" "$SNAP" && chmod +x "$SNAP" || { rm -f "$SNAP"; exit 1; }
  TRAFFIC_INGEST_SNAPSHOT=1
  export TRAFFIC_INGEST_SNAPSHOT
  /bin/sh "$SNAP" "$@"
  rc=$?
  rm -f "$SNAP"
  exit $rc
fi

REPO="${REPO:-/opt/fund-vista}"
API="${API:-http://127.0.0.1:8100}"
ICAOS="${TRAFFIC_ICAOS:-LHBP}"
DAYS="${TRAFFIC_DAYS:-3}"
CONTAINER="${CONTAINER:-fundvista}"
SLICES="$REPO/backend/traffic/slices"

# Recent UTC days (oldest first) in GNU and BSD/macOS date flavors.
days() {
  i="$DAYS"
  while [ "$i" -ge 1 ]; do
    if date -u -d "$i day ago" +%F >/dev/null 2>&1; then
      date -u -d "$i day ago" +%F
    else
      date -u -v-"${i}"d +%F
    fi
    i=$((i - 1))
  done
}

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }

post_job() { # date -> prints job_id, or ERR:<code> when the app refused it
  icaos_json=$(printf '"%s",' $ICAOS | sed 's/,$//')
  curl -s -m 30 -X POST "$API/v1/jobs" -H 'content-type: application/json' \
    -d "{\"type\":\"traffic_ingest\",\"request\":{\"icaos\":[$icaos_json],\"date\":\"$1\"},\"cancel_previous\":false}" |
    python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except ValueError:
    print("ERR:no_response")
    sys.exit(0)
if d.get("job_id"):
    print(d["job_id"])
else:
    print("ERR:" + ((d.get("error") or {}).get("code", "unknown")))'
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

wait_job() { # job_id -> 0 done, 1 failed/cancelled (code in $JOB_ERROR), 2 timeout (6 h)
  JOB_ERROR=""
  i=0
  while [ "$i" -lt 360 ]; do
    st="$(curl -s -m 30 "$API/v1/jobs/$1" |
      python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
    case "$st" in
      done) log "job $1 done"; return 0 ;;
      failed | cancelled)
        log "job $1 $st"
        JOB_ERROR="$(curl -s -m 30 "$API/v1/jobs/$1" |
          python3 -c 'import json,sys; print((json.load(sys.stdin).get("error") or {}).get("code",""))')"
        return 1
        ;;
    esac
    sleep 60
    i=$((i + 1))
  done
  log "job $1 poll timeout"
  return 2
}

FAILED=0
HARVESTED=0
for DAY in $(days); do   # oldest day first
  JOB="$(post_job "$DAY" || true)"
  case "$JOB" in
    ERR:invalid_params)
      # Older app without the icaos shape: one job per airport (one pass each).
      log "multi-airport enqueue unavailable — per-airport fallback for $DAY"
      for ICAO in $ICAOS; do
        log "ingest $ICAO $DAY"
        JOB="$(post_job_one "$ICAO" "$DAY")"
        case "$JOB" in
          [0-9a-f][0-9a-f]* | [0-9A-F][0-9A-F]*) log "job $JOB for $ICAO" ;;
          *) log "POST failed for $ICAO (no job_id)"; FAILED=1; continue ;;
        esac
        if wait_job "$JOB"; then
          HARVESTED=1
        elif [ "$JOB_ERROR" = "traffic_not_available" ]; then
          log "day $DAY: archive not published yet — skipped"
        else
          FAILED=1
        fi
      done
      continue
      ;;
    ERR:*)
      log "day $DAY: enqueue refused (${JOB#ERR:})"
      FAILED=1
      continue
      ;;
  esac
  [ -n "$JOB" ] || { log "POST failed for $DAY (no job_id)"; FAILED=1; continue; }
  # One archive pass serves the whole list (the API accepts icaos).
  log "ingest [$ICAOS] $DAY as job $JOB"
  if wait_job "$JOB"; then
    HARVESTED=1
  elif [ "$JOB_ERROR" = "traffic_not_available" ]; then
    log "day $DAY: archive not published yet — skipped (retried while in the window)"
  else
    log "day $DAY: job failed ($JOB_ERROR)"
    FAILED=1
  fi
done
[ "$HARVESTED" -eq 1 ] || { log "no day harvested — nothing to export"; exit 1; }
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
# An earlier run may have died mid-way and left slices staged. Commit them
# (same content the export below would produce) so the tree is clean for
# the pull — the CD deploy refuses dirty trees, so only this script can
# dirty the checkout.
if [ -n "$(git status --porcelain)" ]; then
  log "leftover changes from an earlier run — committing them"
  git add -A backend/traffic/slices || exit 1
  git commit -m "data(traffic): slices from an earlier run" || true
fi
# CD deploys leave the prod checkout detached at the deployed sha; the git
# export needs a branch. The tree is clean here (deploy-nas.sh refuses dirty
# trees), so checking main out is safe.
if [ -z "$(git symbolic-ref -q HEAD)" ]; then
  git checkout -q main || { log "cannot check out main (detached HEAD)"; exit 1; }
fi
git pull --rebase origin main || { log "git pull failed"; exit 1; }
git add backend/traffic/slices || exit 1
if ! git diff --cached --quiet; then
  git commit -m "data(traffic): daily slices ($ICAOS)" || exit 1
fi
# Push whatever is not on origin yet — the new slice commit, a leftover
# commit made above, or both.
if [ -n "$(git log --oneline origin/main..HEAD 2>/dev/null)" ]; then
  if ! git push origin main; then
    git pull --rebase origin main && git push origin main || { log "git push failed"; exit 1; }
  fi
  log "pushed traffic slices to origin/main"
else
  log "no new slices for the window"
fi
