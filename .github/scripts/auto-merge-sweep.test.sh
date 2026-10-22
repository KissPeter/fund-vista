#!/usr/bin/env bash
#
# Self test of the auto-merge sweep (the pure decision logic).
#
# The sweep talks to the GitHub API, so the API part is verified by a dry run
# against the real repository (`gh workflow run auto-merge.yml -f
# dry_run=true`). This test covers the decision logic that decides whether a
# PR may be merged: the job classification, the gate configuration and the
# merge-ref verdict.
#
# Run: bash .github/scripts/auto-merge-sweep.test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/scripts/auto-merge-sweep.sh
source "$SCRIPT_DIR/auto-merge-sweep.sh"

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

# --- the gate configuration ------------------------------------------------

expect "the workflow name of an entry" "backend-tests" \
  "$(gate_workflow_name "backend-tests=integration+schemathesis")"

expect "the required jobs of an entry" "integration schemathesis" \
  "$(gate_workflow_jobs "backend-tests=integration+schemathesis")"

expect "an entry without a job list" "CI" "$(gate_workflow_name "CI")"

expect "an entry without a job list has no jobs" "" "$(gate_workflow_jobs "CI")"

expect "a single required job" "checks" "$(gate_workflow_jobs "frontend-checks=checks")"

# --- the job classification ------------------------------------------------

expect "no jobs at all" "missing" "$(classify_jobs "" "gate" "live-e2e")"

expect "gate green" "green" "$(classify_jobs "$(printf 'gate\tcompleted\tsuccess')" "gate" "live-e2e")"

expect "gate green, advisory job skipped" "green" \
  "$(classify_jobs "$(printf 'gate\tcompleted\tsuccess\nlive-e2e\tcompleted\tskipped')" "gate" "live-e2e")"

expect "gate green, advisory job red (advisory never blocks)" "green" \
  "$(classify_jobs "$(printf 'gate\tcompleted\tsuccess\nlive-e2e\tcompleted\tfailure')" "gate" "live-e2e")"

expect "gate red" "red" \
  "$(classify_jobs "$(printf 'gate\tcompleted\tfailure\nlive-e2e\tcompleted\tskipped')" "gate" "live-e2e")"

expect "gate still running" "pending" \
  "$(classify_jobs "$(printf 'gate\tin_progress\tnone')" "gate" "live-e2e")"

expect "the required job is missing" "missing" \
  "$(classify_jobs "$(printf 'other\tcompleted\tsuccess')" "gate" "live-e2e")"

expect "another job fails" "red" \
  "$(classify_jobs "$(printf 'gate\tcompleted\tsuccess\nother\tcompleted\tfailure')" "gate" "live-e2e")"

expect "another job is neutral" "green" \
  "$(classify_jobs "$(printf 'gate\tcompleted\tsuccess\nother\tcompleted\tneutral')" "gate" "live-e2e")"

expect "a re-run of the gate decides" "red" \
  "$(classify_jobs "$(printf 'gate\tcompleted\tsuccess\ngate\tcompleted\tfailure')" "gate" "live-e2e")"

expect "an advisory job that still runs does not block" "green" \
  "$(classify_jobs "$(printf 'gate\tcompleted\tsuccess\nlive-e2e\tin_progress\tnone')" "gate" "live-e2e")"

expect "a blocking job that still runs blocks" "pending" \
  "$(classify_jobs "$(printf 'gate\tin_progress\tnone\nlive-e2e\tcompleted\tskipped')" "gate" "live-e2e")"

expect "two required jobs, both green" "green" \
  "$(classify_jobs "$(printf 'integration\tcompleted\tsuccess\nschemathesis\tcompleted\tsuccess')" "integration schemathesis" "live-e2e")"

expect "two required jobs, one red" "red" \
  "$(classify_jobs "$(printf 'integration\tcompleted\tsuccess\nschemathesis\tcompleted\tfailure')" "integration schemathesis" "live-e2e")"

expect "two required jobs, one missing" "missing" \
  "$(classify_jobs "$(printf 'integration\tcompleted\tsuccess')" "integration schemathesis" "live-e2e")"

# --- the merge-ref verdict -------------------------------------------------

expect "no status at all" "" \
  "$(parse_merge_ref_status '{"statuses":[]}' "automerge/merge-ref/backend-tests")"

expect "no status with that context" "" \
  "$(parse_merge_ref_status '{"statuses":[{"context":"other","state":"success","description":"merge ref abc","created_at":"2026-10-22T10:00:00Z"}]}' "automerge/merge-ref/backend-tests")"

expect "a green merge-ref status" "success abc123" \
  "$(parse_merge_ref_status '{"statuses":[{"context":"automerge/merge-ref/backend-tests","state":"success","description":"merge ref abc123","created_at":"2026-10-22T10:00:00Z"}]}' "automerge/merge-ref/backend-tests")"

expect "the context of another workflow is not read" "" \
  "$(parse_merge_ref_status '{"statuses":[{"context":"automerge/merge-ref/frontend-checks","state":"success","description":"merge ref abc123","created_at":"2026-10-22T10:00:00Z"}]}' "automerge/merge-ref/backend-tests")"

expect "a red merge-ref status" "failure abc123" \
  "$(parse_merge_ref_status '{"statuses":[{"context":"automerge/merge-ref/backend-tests","state":"failure","description":"merge ref abc123","created_at":"2026-10-22T10:00:00Z"}]}' "automerge/merge-ref/backend-tests")"

expect "the newest merge-ref status wins" "success def456" \
  "$(parse_merge_ref_status '{"statuses":[{"context":"automerge/merge-ref/backend-tests","state":"success","description":"merge ref abc123","created_at":"2026-10-22T10:00:00Z"},{"context":"automerge/merge-ref/backend-tests","state":"success","description":"merge ref def456","created_at":"2026-10-22T11:00:00Z"}]}' "automerge/merge-ref/backend-tests")"

expect "no status" "none" "$(merge_ref_verdict "" "abc123")"

expect "a green status on the current merge ref" "green" \
  "$(merge_ref_verdict "success abc123" "abc123")"

expect "a green status on an older merge ref" "stale" \
  "$(merge_ref_verdict "success abc123" "def456")"

expect "a red status" "red" "$(merge_ref_verdict "failure abc123" "abc123")"

expect "an error status" "red" "$(merge_ref_verdict "error abc123" "abc123")"

# --- the block labels ------------------------------------------------------

expect "no label" "1" "$(has_block_label $'bug' && echo 0 || echo 1)"

expect "block label" "0" "$(has_block_label $'bug\nautomerge:block' && echo 0 || echo 1)"

expect "needs-human label" "0" "$(has_block_label $'needs-human' && echo 0 || echo 1)"

expect "a label that only looks similar" "1" \
  "$(has_block_label $'automerge:blocked' && echo 0 || echo 1)"

expect "no label at all" "1" "$(has_block_label "" && echo 0 || echo 1)"

# --- the configuration -----------------------------------------------------

MERGE_METHOD="squash"
expect "a known merge method is accepted" "squash" "$MERGE_METHOD"

MERGE_METHOD="nope"
if (validate_config) >/dev/null 2>&1; then
  expect "an unknown merge method is rejected" "rejected" "accepted"
else
  expect "an unknown merge method is rejected" "rejected" "rejected"
fi

# --- a broken sweep fails, it does not report green -------------------------

# `fail` must end the shell with a non-zero status and an error annotation.
status=0
out="$(fail "boom" 2>&1)" || status=$?
expect "fail annotates the error" "::error::boom" "$out"
expect "fail exits non-zero" "1" "$status"

# preflight without gh on the PATH is an error, not a skipped sweep.
status=0
out="$(/bin/bash -c 'source "$1"; REPO=o/r; command() { [ "$2" = gh ] && return 1; builtin command "$@"; }; preflight' _ "$SCRIPT_DIR/auto-merge-sweep.sh" 2>&1)" || status=$?
expect "preflight rejects a runner without gh" "1" "$status"
expect "preflight names the missing tool" "::error::gh is not installed on this runner" "$out"

# a failed PR is counted, a skipped one is not.
FAILED_COUNT=0; SKIPPED_COUNT=0; MERGED_COUNT=0; SUMMARY_ROWS=""
add_row "#1" "t" "failed" "d"
add_row "#2" "t" "skipped" "d"
expect "a failed row is counted as failed" "1/1" "$FAILED_COUNT/$SKIPPED_COUNT"

# --- the sweep runs to the end and exits 0 ----------------------------------
# Regression: the EXIT trap used a `local` of main(), so every sweep that got
# past the main-is-green check ended with "unbound variable" and a red job.
shim_dir="$(mktemp -d)"
cat >"$shim_dir/gh" <<'SHIM'
#!/usr/bin/env bash
case "$*" in
  *"--jq .full_name"* | *"--jq '.full_name'"*) echo "o/r" ;;
  *"commits/main"*) echo "abc123" ;;
  *"actions/runs?head_sha"*) echo '{"workflow_runs":[]}' ;;
  "pr list"*) echo '[]' ;;
  *) echo '{}' ;;
esac
SHIM
chmod +x "$shim_dir/gh"
status=0
out="$(PATH="$shim_dir:$PATH" GITHUB_REPOSITORY=o/r AUTOMERGE_GATE_WORKFLOWS=ci /bin/bash \
  "$SCRIPT_DIR/auto-merge-sweep.sh" 2>&1)" || status=$?
expect "a sweep over no PRs exits 0" "0" "$status"
expect "a sweep over no PRs reaches the end" "1" "$(echo "$out" | grep -c 'sweep done')"
rm -rf "$shim_dir"

echo ""
if [ "$FAILURES" -gt 0 ]; then
  echo "$FAILURES of $CHECKS checks FAILED"
  exit 1
fi
echo "all $CHECKS checks passed"
