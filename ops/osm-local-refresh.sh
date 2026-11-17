#!/bin/sh
# Download the Geofabrik Hungary extract and (re)build the local OSM
# database the backend reads (CR-010). Run on the Mac host; safe to re-run.
# Scheduled monthly by ops/osm-local-refresh.plist. The backend picks the
# new file up without a restart; a failed run leaves the old file in place.
#
#   ops/osm-local-refresh.sh
#
# Builds from a clean export of origin/main (fetched first), so it does not
# depend on which branch or local edits the Mac's checkout has.
# Env: FV_REPO (git checkout, default ~/srv/fund-vista), FV_DATA_DIR (the
# host directory bind-mounted as /data), OSM_REGION (Geofabrik path,
# default europe/hungary).
set -eu
# launchd gives a minimal PATH; uv and git live here.
PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
REPO="${FV_REPO:-$HOME/srv/fund-vista}"
DATA_DIR="${FV_DATA_DIR:-/Users/kissp/srv/fund-vista-data}"
REGION="${OSM_REGION:-europe/hungary}"
NAME="${REGION##*/}"
OUT="$DATA_DIR/osm-local"
log() { echo "$(date -u +%FT%TZ) $*"; }
[ -f "$DATA_DIR/.fund-vista-data" ] || { echo "no .fund-vista-data marker in $DATA_DIR" >&2; exit 1; }
mkdir -p "$OUT"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/osm-local.XXXXXX")"   # downloads are untrusted data
trap 'rm -rf "$WORK"' EXIT
mkdir "$WORK/dl" "$WORK/src"
log "export origin/main"
git -C "$REPO" fetch -q origin main
git -C "$REPO" archive origin/main backend | tar -x -C "$WORK/src"
log "download $REGION"
curl -fsSL -A "fund-vista-citymap/1.0" -o "$WORK/dl/$NAME.osm.pbf" "https://download.geofabrik.de/$REGION-latest.osm.pbf"
curl -fsSL -A "fund-vista-citymap/1.0" -o "$WORK/dl/$NAME.poly" "https://download.geofabrik.de/$REGION.poly"
log "build"
cd "$WORK/src"
PYTHONPATH="$WORK/src" uv run --project backend --with osmium python -m backend.osm_local.build \
  "$WORK/dl/$NAME.osm.pbf" "$OUT/osm-hu.sqlite" --poly "$WORK/dl/$NAME.poly"
log "built $OUT/osm-hu.sqlite"
