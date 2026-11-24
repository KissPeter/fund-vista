#!/usr/bin/env bash
#
# Self test of the backend-tests change detection (the pure decision logic).
#
# Run: bash .github/scripts/backend-changes.test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/scripts/backend-changes.sh
source "$SCRIPT_DIR/backend-changes.sh"

FAILURES=0
CHECKS=0

# expect <name> <expected> <actual>
expect() {
  CHECKS=$((CHECKS + 1))
  if [ "$2" != "$3" ]; then
    FAILURES=$((FAILURES + 1))
    echo "FAIL: $1 - expected '$2', got '$3'"
  else
    echo "ok: $1 ($3)"
  fi
}

# verdict <decide args...> -> the true|false part of the decision
verdict() {
  local r
  r="$(decide "$@")"
  echo "${r%%$'\t'*}"
}

# --- the backend paths -----------------------------------------------------

expect "a backend file" "true" "$(backend_changed "backend/app.py")"
expect "a nested backend file" "true" "$(backend_changed "backend/tests/test_x.py")"
expect "runtests.sh" "true" "$(backend_changed "runtests.sh")"
expect "the schemathesis smoke script" "true" "$(backend_changed "ops/schemathesis-smoke.sh")"
expect "the workflow itself" "true" "$(backend_changed ".github/workflows/backend-tests.yml")"
expect "this detection script" "true" "$(backend_changed ".github/scripts/backend-changes.sh")"
expect "a docs file" "false" "$(backend_changed "docs/auto-merge.md")"
expect "a frontend file" "false" "$(backend_changed "src/App.tsx")"
expect "a path that only starts like backend" "false" "$(backend_changed "backend-notes.md")"
expect "another ops script" "false" "$(backend_changed "ops/deploy.sh")"
expect "the sweep script" "false" "$(backend_changed ".github/scripts/auto-merge-sweep.sh")"
expect "no files" "false" "$(backend_changed "")"
expect "one backend file among others" "true" \
  "$(backend_changed "$(printf 'docs/a.md\nbackend/x.py\nsrc/y.ts')")"

# --- the decision ----------------------------------------------------------

expect "a docs-only PR skips the suite" "false" \
  "$(verdict pull_request 7 "" "docs/a.md" 1 0)"
expect "a backend PR runs the suite" "true" \
  "$(verdict pull_request 7 "" "backend/x.py" 1 0)"
expect "an unreadable PR file list runs the suite (fail closed)" "true" \
  "$(verdict pull_request 7 "" "" 0 0)"
expect "a manual run without a PR runs the suite" "true" \
  "$(verdict workflow_dispatch "" "" "" 0 0)"
expect "a dispatched merge-ref run of a docs-only PR skips the suite" "false" \
  "$(verdict workflow_dispatch 7 "" "docs/a.md" 1 0)"
expect "a dispatched merge-ref run of a backend PR runs the suite" "true" \
  "$(verdict workflow_dispatch 7 "" "backend/x.py" 1 0)"
expect "a docs-only push skips the suite" "false" \
  "$(verdict push "" abc "docs/a.md" 1 1)"
expect "a backend push runs the suite" "true" \
  "$(verdict push "" abc "backend/x.py" 1 1)"
expect "a push without a previous commit runs the suite" "true" \
  "$(verdict push "" 0000000000000000000000000000000000000000 "docs/a.md" 1 1)"
expect "a push with an empty previous commit runs the suite" "true" \
  "$(verdict push "" "" "docs/a.md" 1 1)"
expect "a push at the compare file limit runs the suite" "true" \
  "$(verdict push "" abc "docs/a.md" 1 300)"
expect "an unknown event runs the suite" "true" \
  "$(verdict merge_group "" "" "docs/a.md" 1 0)"

echo ""
if [ "$FAILURES" -gt 0 ]; then
  echo "$FAILURES of $CHECKS checks FAILED"
  exit 1
fi
echo "all $CHECKS checks passed"
