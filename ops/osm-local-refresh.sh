#!/bin/sh
# Download the Geofabrik Hungary extract and (re)build the local OSM
# database the backend reads (CR-010). Run on the Mac host; safe to re-run
# (monthly). The backend picks the new file up without a restart.
#
#   ops/osm-local-refresh.sh
#
# Env: FV_REPO (checkout, default ~/srv/fund-vista), FV_DATA_DIR (the host
# directory bind-mounted as /data), OSM_REGION (Geofabrik path, default
# europe/hungary).
set -eu
REPO="${FV_REPO:-$HOME/srv/fund-vista}"
DATA_DIR="${FV_DATA_DIR:-/Users/kissp/srv/fund-vista-data}"
REGION="${OSM_REGION:-europe/hungary}"
NAME="${REGION##*/}"
OUT="$DATA_DIR/osm-local"
[ -f "$DATA_DIR/.fund-vista-data" ] || { echo "no .fund-vista-data marker in $DATA_DIR" >&2; exit 1; }
mkdir -p "$OUT"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/osm-local.XXXXXX")"   # downloads are untrusted data
trap 'rm -rf "$WORK"' EXIT
curl -fsSL -A "fund-vista-citymap/1.0" -o "$WORK/$NAME.osm.pbf" "https://download.geofabrik.de/$REGION-latest.osm.pbf"
curl -fsSL -A "fund-vista-citymap/1.0" -o "$WORK/$NAME.poly" "https://download.geofabrik.de/$REGION.poly"
cd "$REPO"
PYTHONPATH="$REPO" uv run --project backend --with osmium python -m backend.osm_local.build \
  "$WORK/$NAME.osm.pbf" "$OUT/osm-hu.sqlite" --poly "$WORK/$NAME.poly"
echo "built $OUT/osm-hu.sqlite"
