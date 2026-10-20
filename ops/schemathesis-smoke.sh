#!/usr/bin/env sh
# Schemathesis smoke: OpenAPI property fuzzing against the fully mocked app.
#
# The server runs with FUNDVISTA_UPSTREAM_MODE=mock — every third-party call
# (OurAirports, Overpass, OSM API, Nominatim, GitHub release index/archive) is
# served from deterministic in-repo fixtures and any unmocked host FAILS
# CLOSED, so the whole run is hermetic: no network, no real data.
#
# Ported from affilio's run_schemathesis_local.sh (CircleCI there; plain GH
# Actions step here). Lightweight by design: one pass, not_a_server_error
# only, capped examples per operation, junit report artifact on failure.
#
# Excluded: the catch-all proxy /{prefix}/{path} — it forwards to real
# third-party hosts (kh.hu/Erste/frankfurter) which have no mock fixtures by
# design; unknown prefixes 404, real ones would fail closed (RuntimeError →
# raw 500) rather than map to 502. That class gets dedicated pytest mocks
# (backend/tests/test_proxy.py) instead of property fuzzing.
#
# Usage:
#   ops/schemathesis-smoke.sh [workspace_dir]   # starts/stops its own server
#   ops/schemathesis-smoke.sh --no-start        # reuse an already-running one
set -eu

WORK="${1:-.}"
NO_START=0
case "$WORK" in --no-start) NO_START=1; WORK="." ;; esac

HOST="${SCHEMATHESIS_HOST:-127.0.0.1}"
PORT="${SCHEMATHESIS_PORT:-8901}"
BASE_URL="http://${HOST}:${PORT}"
SCHEMA="${BASE_URL}/openapi.json"
PYTHON="${SCHEMATHESIS_PYTHON:-python}"
REPORT_DIR="${SCHEMATHESIS_REPORT_DIR:-$WORK/schemathesis_results}"

ST="${SCHEMATHESIS_CLI:-}"
if [ -z "$ST" ]; then
  # Default to the CLI installed beside the chosen interpreter (venv), then PATH.
  for candidate in "$(dirname "$PYTHON")/schemathesis" "$(dirname "$PYTHON")/st" "$(command -v st 2>/dev/null)" "$(command -v schemathesis 2>/dev/null)"; do
    if [ -n "$(command -v "$candidate" 2>/dev/null)" ] || [ -x "$candidate" ]; then
      ST="$candidate"; break
    fi
  done
fi
if [ -z "$ST" ]; then
  echo "ERROR: schemathesis CLI not found (pip install schemathesis)" >&2
  exit 2
fi

if [ "$NO_START" = "0" ]; then
  # WORK may be a fresh caller-provided dir (CI) that doesn't exist yet.
  mkdir -p "$WORK"
  # Same-process env the pytest suite uses (see backend/tests/conftest.py):
  # hermetic Redis (in-process fallback), rate limit effectively off, tmp
  # data dir, signing key so POST /v1/tokens exercises the HMAC path.
  DATA_DIR="$(mktemp -d)"
  PENPLOT_DATA_DIR="$DATA_DIR" \
  FUNDVISTA_UPSTREAM_MODE=mock \
  REDIS_CLOUD_URL=redis://127.0.0.1:1/0 \
  PENPLOT_RATE_LIMIT=100000 \
  PENPIXEL_HMAC_SECRET="${PENPIXEL_HMAC_SECRET:-test-secret-smoke}" \
  LOG_LEVEL=WARNING \
  PYTHONPATH=. \
  "$PYTHON" -m uvicorn backend.main:app --host "$HOST" --port "$PORT" \
    > "$WORK/smoke-server.log" 2>&1 &
  PID=$!
  echo "==> Server PID ${PID}, probing ${BASE_URL}/healthz …"
  OK=0
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/healthz" 2>/dev/null)" = "200" ]; then OK=1; break; fi
    sleep 1
  done
  if [ "$OK" != "1" ]; then
    echo "ERROR: server never became healthy" >&2
    tail -40 "$WORK/smoke-server.log" >&2 || true
    exit 2
  fi
else
  echo "==> Reusing pre-running server at ${BASE_URL}"
fi
trap '[ "$NO_START" = "0" ] && { kill "$PID" 2>/dev/null || true; } || true' EXIT

mkdir -p "$REPORT_DIR"
JUNIT="$REPORT_DIR/schemathesis_smoke.xml"

echo "==> Schemathesis smoke run …"
set +e
# Wheels: single pass, 10 examples per operation, only the never-5xx check
# (4xx envelopes and their schema mismatches are NOT smoke failures — the
# pytest integration suite owns negative coverage class by class).
"$ST" run \
  --checks not_a_server_error \
  --max-failures 10 \
  --max-examples 10 \
  --wait-for-schema 10 \
  --workers 1 \
  --exclude-path-regex '^/\{prefix\}/\{path\}$' \
  --report junit \
  --report-junit-path "$JUNIT" \
  "$SCHEMA"
EXIT=$?
set -e

# Network hiccups / schema-fetch failures can produce a nonzero CLI exit
# without real failures — count failures/errors in the junit XML
# (affilio pattern).
FAILS=$("$PYTHON" - "$JUNIT" <<'EOF'
import sys, xml.etree.ElementTree as ET
total = 0
try:
    root = ET.parse(sys.argv[1]).getroot()
    for suite in root.iter("testsuite"):
        total += int(suite.get("failures", 0)) + int(suite.get("errors", 0))
except Exception:
    pass
print(total)
EOF
)

echo
echo "==> JUnit failures: ${FAILS}"
if [ ! -s "$JUNIT" ]; then
  echo "❌ Schemathesis produced no junit report — hard failure (exit ${EXIT})"
  echo "   Server log: $WORK/smoke-server.log"
  exit 1
fi
if [ "$FAILS" != "0" ]; then
  echo "❌ Schemathesis smoke found ${FAILS} server-error case(s)"
  echo "   Report: $JUNIT (CI uploads it as an artifact)"
  exit 1
fi
if [ "$EXIT" = "2" ]; then
  echo "❌ Schemathesis CLI usage error (exit 2) — fix the invocation, never ignore"
  exit 2
fi
if [ "$EXIT" != "0" ]; then
  echo "⚠️ Schemathesis exited ${EXIT} with 0 junit failures — inspect $JUNIT (CI uploads it)"
fi
echo "✅ Schemathesis smoke: all operations 5xx-free under mock mode"
