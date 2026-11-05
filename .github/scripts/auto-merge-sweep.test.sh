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

BCTX="automerge/merge-ref/backend-tests"
expect "no status at all" "" \
  "$(parse_merge_ref_status '{"statuses":[]}' "$BCTX")"

expect "no status with that context" "" \
  "$(parse_merge_ref_status '{"statuses":[{"context":"other","state":"success","description":"head a main b","created_at":"2026-10-22T10:00:00Z"}]}' "$BCTX")"

expect "a green merge-ref status" "success"$'\t'"head aaaa main bbbb" \
  "$(parse_merge_ref_status '{"statuses":[{"context":"automerge/merge-ref/backend-tests","state":"success","description":"head aaaa main bbbb","created_at":"2026-10-22T10:00:00Z"}]}' "$BCTX")"

expect "the context of another workflow is not read" "" \
  "$(parse_merge_ref_status '{"statuses":[{"context":"automerge/merge-ref/frontend-checks","state":"success","description":"head aaaa main bbbb","created_at":"2026-10-22T10:00:00Z"}]}' "$BCTX")"

expect "the newest merge-ref status wins" "success"$'\t'"head aaaa main dddd" \
  "$(parse_merge_ref_status '{"statuses":[{"context":"automerge/merge-ref/backend-tests","state":"success","description":"head aaaa main bbbb","created_at":"2026-10-22T10:00:00Z"},{"context":"automerge/merge-ref/backend-tests","state":"success","description":"head aaaa main dddd","created_at":"2026-10-22T11:00:00Z"}]}' "$BCTX")"

T=$'\t'
expect "no status" "none" "$(merge_ref_verdict "" aaaa bbbb cccc 1)"
expect "green on the current head and main" "green" \
  "$(merge_ref_verdict "success${T}head aaaa main bbbb" aaaa bbbb cccc 0)"
expect "green on an older main is stale" "stale" \
  "$(merge_ref_verdict "success${T}head aaaa main oldd" aaaa bbbb cccc 0)"
expect "green on an older head is stale" "stale" \
  "$(merge_ref_verdict "success${T}head oldd main bbbb" aaaa bbbb cccc 0)"
expect "red on the current head and main" "red" \
  "$(merge_ref_verdict "failure${T}head aaaa main bbbb" aaaa bbbb cccc 0)"
expect "red on an older main is stale, it must not block" "stale" \
  "$(merge_ref_verdict "failure${T}head aaaa main oldd" aaaa bbbb cccc 0)"
expect "an error status on the current head and main" "red" \
  "$(merge_ref_verdict "error${T}head aaaa main bbbb" aaaa bbbb cccc 0)"
expect "a pending status is none" "none" \
  "$(merge_ref_verdict "pending${T}head aaaa main bbbb" aaaa bbbb cccc 0)"
expect "the old merge-ref form counts on a ref built on the current main" "green" \
  "$(merge_ref_verdict "success${T}merge ref cccc" aaaa bbbb cccc 1)"
expect "the old merge-ref form is stale on a ref built on an older main" "stale" \
  "$(merge_ref_verdict "success${T}merge ref cccc" aaaa bbbb cccc 0)"
expect "an unknown description is stale" "stale" \
  "$(merge_ref_verdict "success${T}whatever" aaaa bbbb cccc 1)"

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

# --- the main gate: pending is not red -------------------------------------
# A shim gh answers the main gate: SHIM_STATE is success, failure or pending;
# a pending-once flag file makes the first job check pending and the next green.
main_shim="$(mktemp -d)"
cat >"$main_shim/gh" <<'SHIM'
#!/usr/bin/env bash
case "$*" in
  *"--jq .full_name"* | *"--jq '.full_name'"*) echo "o/r" ;;
  *"commits/main"*) echo "abc123" ;;
  *"actions/runs/777/jobs"*)
    state="${SHIM_STATE:-success}"
    if [ -f "$SHIM_DIR/pending-once" ]; then
      rm -f "$SHIM_DIR/pending-once"
      state=pending
    fi
    case "$state" in
      pending) printf 'gate\tin_progress\tnone\n' ;;
      *) printf 'gate\tcompleted\t%s\n' "$state" ;;
    esac
    ;;
  *"actions/runs?head_sha"*) echo '{"workflow_runs":[{"id":777,"name":"CI","run_number":1}]}' ;;
  "pr list"*) echo '[]' ;;
  *) echo '{}' ;;
esac
SHIM
chmod +x "$main_shim/gh"
export SHIM_DIR="$main_shim"
export GITHUB_REPOSITORY=o/r REPO=o/r
GATE_WORKFLOW_LIST="CI=gate"

expect "a green main gate is green" "green" \
  "$(PATH="$main_shim:$PATH" SHIM_STATE=success main_gate_state 2>/dev/null)"

expect "a red main gate is red" "red" \
  "$(PATH="$main_shim:$PATH" SHIM_STATE=failure main_gate_state 2>/dev/null)"

expect "a running main gate is pending, not red" "pending" \
  "$(PATH="$main_shim:$PATH" SHIM_STATE=pending main_gate_state 2>/dev/null)"

expect "a pending main gate that turns green is waited for" "green" \
  "$(touch "$main_shim/pending-once"; PATH="$main_shim:$PATH" MAIN_WAIT_SECONDS=5 MAIN_POLL_SECONDS=0 \
    SHIM_STATE=success wait_for_main 2>/dev/null)"

expect "a main gate still pending at the timeout stays pending" "pending" \
  "$(PATH="$main_shim:$PATH" MAIN_WAIT_SECONDS=0 MAIN_POLL_SECONDS=0 \
    SHIM_STATE=pending wait_for_main 2>/dev/null)"

# The sweep: a pending main ends with the notice and exit 0, a red main with
# the warning only.
status=0
out="$(PATH="$main_shim:$PATH" SHIM_STATE=pending AUTOMERGE_MAIN_WAIT_SECONDS=0 AUTOMERGE_MAIN_POLL_SECONDS=0 \
  AUTOMERGE_GATE_WORKFLOWS=CI=gate /bin/bash "$SCRIPT_DIR/auto-merge-sweep.sh" 2>&1)" || status=$?
expect "a sweep that times out on a pending main exits 0" "0" "$status"
expect "a sweep that times out on a pending main gives a notice" "1" \
  "$(echo "$out" | grep -c '::notice::the sweep timed out waiting for main')"
expect "a sweep that times out on a pending main merges nothing" "0" \
  "$(echo "$out" | grep -c 'merged #')"

status=0
out="$(PATH="$main_shim:$PATH" SHIM_STATE=failure AUTOMERGE_GATE_WORKFLOWS=CI=gate /bin/bash \
  "$SCRIPT_DIR/auto-merge-sweep.sh" 2>&1)" || status=$?
expect "a sweep on a red main exits 0" "0" "$status"
expect "a sweep on a red main gives no timeout notice" "0" \
  "$(echo "$out" | grep -c 'timed out')"
expect "a sweep on a red main warns" "1" \
  "$(echo "$out" | grep -c '::warning::main is not green (red)')"
rm -rf "$main_shim"
unset SHIM_DIR GATE_WORKFLOW_LIST

# --- the per-PR decisions (a shim gh) ----------------------------------------
# The shim answers the API calls decide_pr and the dispatch helpers make and
# logs every dispatch (`workflow run`) and comment write in $SHIM_LOG.
# Fixture SHAs: head aaaa1111, main bbbb2222, GitHub merge ref cccc3333.
pr_shim="$(mktemp -d)"
cat >"$pr_shim/gh" <<'SHIM'
#!/usr/bin/env bash
jq_expr=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  if [ "${args[$i]}" = "--jq" ]; then jq_expr="${args[$((i + 1))]}"; fi
done
out() {
  if [ -n "$jq_expr" ]; then jq -r "$jq_expr" <<<"$1"; else printf '%s\n' "$1"; fi
}
case "$*" in
  "workflow run"*) echo "$*" >>"$SHIM_LOG" ;;
  *"-X POST"*) echo "$*" >>"$SHIM_LOG" ;;
  *"-X PATCH"*) echo "$*" >>"$SHIM_LOG" ;;
  "pr view"*) [ -n "${SHIM_PRVIEW_FAIL:-}" ] && exit 1; printf 'src/a.ts\n' ;;
  *"/pulls/7"*) out "{\"mergeable\":true,\"mergeable_state\":\"${SHIM_MSTATE:-clean}\",\"head\":{\"sha\":\"aaaa1111\"}}" ;;
  *"git/ref/pull/7/merge"*) out '{"object":{"sha":"cccc3333"}}' ;;
  *"commits/cccc3333"*) out "{\"parents\":[{\"sha\":\"${SHIM_REF_PARENT:-bbbb2222}\"}]}" ;;
  *"commits/main"*) out '{"sha":"bbbb2222"}' ;;
  *"commits/aaaa1111/status?"*) out "$(cat "$SHIM_STATUSES")" ;;
  *"commits/aaaa1111/status"*) out '{"total_count":0}' ;;
  *"actions/runs?"*) out "$(cat "$SHIM_RUNS")" ;;
  *"issues/7/comments"*) out "$(cat "$SHIM_COMMENTS")" ;;
  *) out '{}' ;;
esac
SHIM
chmod +x "$pr_shim/gh"
export SHIM_LOG="$pr_shim/log" SHIM_STATUSES="$pr_shim/statuses" SHIM_RUNS="$pr_shim/runs" SHIM_COMMENTS="$pr_shim/comments"
REPO=o/r
MAIN_SHA=bbbb2222
GATE_WORKFLOW_LIST="backend-tests=integration"
ADVISORY_CHECKS=""
BLOCKED_PATHS=""
BLOCK_LABELS="automerge:block,needs-human"
DRY_RUN=false
PATH="$pr_shim:$PATH"
PR7='{"number":7,"isDraft":false,"labels":[],"headRefOid":"aaaa1111"}'

reset_shim() {
  : >"$SHIM_LOG"
  echo '{"statuses":[]}' >"$SHIM_STATUSES"
  echo '{"workflow_runs":[]}' >"$SHIM_RUNS"
  : >"$SHIM_COMMENTS"
  unset SHIM_MSTATE SHIM_REF_PARENT SHIM_PRVIEW_FAIL
}
dispatches() { grep -c '^workflow run' "$SHIM_LOG" || true; }
GREEN_STATUS='{"statuses":[{"context":"automerge/merge-ref/backend-tests","state":"success","description":"head aaaa1111 main bbbb2222","created_at":"2026-10-22T10:00:00Z"}]}'
RED_STATUS='{"statuses":[{"context":"automerge/merge-ref/backend-tests","state":"failure","description":"head aaaa1111 main bbbb2222","created_at":"2026-10-22T10:00:00Z"}]}'
OLD_RED_STATUS='{"statuses":[{"context":"automerge/merge-ref/backend-tests","state":"failure","description":"head aaaa1111 main oldmain","created_at":"2026-10-22T10:00:00Z"}]}'

# 1. `unstable` is not a skip reason: the gate verdict decides.
reset_shim
echo "$GREEN_STATUS" >"$SHIM_STATUSES"
SHIM_MSTATE=unstable
export SHIM_MSTATE
out="$(decide_pr "$PR7" 2>/dev/null)"
expect "an unstable PR with a green verdict for this head and main is merged" "merge" "${out%%|*}"
expect "the merge detail names head and main" "1" \
  "$(echo "$out" | grep -c 'head aaaa1111 and main bbbb2222')"

# 1b. a stale red native run (tested an older main) does not block.
reset_shim
echo "$GREEN_STATUS" >"$SHIM_STATUSES"
echo '{"workflow_runs":[{"id":5,"name":"backend-tests","event":"pull_request","run_number":3,"head_sha":"aaaa1111","pull_requests":[{"head":{"sha":"aaaa1111"},"base":{"sha":"oldmain"}}]}]}' >"$SHIM_RUNS"
SHIM_MSTATE=unstable
export SHIM_MSTATE
out="$(decide_pr "$PR7" 2>/dev/null)"
expect "a red native run against an older main does not block" "merge" "${out%%|*}"

# dirty and blocked are still skips.
reset_shim
SHIM_MSTATE=dirty
export SHIM_MSTATE
expect "a dirty PR is skipped" "skip|conflict with main" "$(decide_pr "$PR7" 2>/dev/null)"
SHIM_MSTATE=blocked
export SHIM_MSTATE
expect "a blocked PR is skipped" "skip|blocked by the repository rules" "$(decide_pr "$PR7" 2>/dev/null)"

# 2. a GitHub merge ref built on an older main is not trusted; a verdict
# naming an older main is stale and triggers a dispatch with main_sha.
reset_shim
echo "$OLD_RED_STATUS" >"$SHIM_STATUSES"
SHIM_REF_PARENT=oldmain
export SHIM_REF_PARENT
out="$(decide_pr "$PR7" 2>/dev/null)"
expect "a verdict about an older main is stale and dispatches" "skip" "${out%%|*}"
expect "the dispatch passes pr_number and main_sha" "1" \
  "$(grep -c 'workflow run backend-tests .*-f pr_number=7 -f main_sha=bbbb2222' "$SHIM_LOG")"

# a red verdict for the current head and main is final: no dispatch.
reset_shim
echo "$RED_STATUS" >"$SHIM_STATUSES"
out="$(decide_pr "$PR7" 2>/dev/null)"
expect "a red verdict for this head and main skips" "skip|the dispatched backend-tests gate is red" "$out"
expect "a red verdict is not re-dispatched" "0" "$(dispatches)"

# the refresh pass honours the same rule (the self re-dispatch loop).
reset_shim
echo "$RED_STATUS" >"$SHIM_STATUSES"
TOKEN_SOURCE=bot DISPATCH_CI=auto MAX_DISPATCH=3 MERGED_COUNT=0
list_open_prs() { echo "[$PR7]"; }
refresh_ci_after_merges 2>/dev/null
expect "the refresh pass does not re-dispatch a red verdict for this head and main" "0" "$(dispatches)"
reset_shim
refresh_ci_after_merges 2>/dev/null
expect "the refresh pass dispatches a PR without a verdict" "1" "$(dispatches)"
reset_shim
DISPATCHED_PRS=" 7 "
refresh_ci_after_merges 2>/dev/null
expect "the refresh pass skips a PR this sweep already dispatched" "0" "$(dispatches)"
DISPATCHED_PRS=" "

# 3. PRs that can never be merged get no dispatch.
reset_shim
out="$(decide_pr '{"number":7,"isDraft":true,"labels":[],"headRefOid":"aaaa1111"}' 2>/dev/null)"
expect "a draft is skipped" "skip|draft" "$out"
out="$(decide_pr '{"number":7,"isDraft":false,"labels":[{"name":"automerge:block"}],"headRefOid":"aaaa1111"}' 2>/dev/null)"
expect "a block label is skipped" "skip|block label" "$out"
BLOCKED_PATHS="src/"
out="$(decide_pr "$PR7" 2>/dev/null)"
expect "a PR touching a blocked path is skipped" "skip|the PR changes a protected path" "$out"
SHIM_PRVIEW_FAIL=1
export SHIM_PRVIEW_FAIL
out="$(decide_pr "$PR7" 2>/dev/null)"
expect "an unreadable file list fails closed" "skip|could not read the PR's changed files" "$out"
refresh_ci_after_merges 2>/dev/null
expect "none of them dispatched a gate" "0" "$(dispatches)"
BLOCKED_PATHS=""
unset SHIM_PRVIEW_FAIL

# 4. the running-gate check counts only the gate workflow's runs.
reset_shim
echo '{"workflow_runs":[{"id":9,"name":"Auto merge","event":"push","status":"in_progress","display_title":"sweep","head_sha":"bbbb2222"}]}' >"$SHIM_RUNS"
expect "the sweep's own run on main is not a running gate" "1" \
  "$(active_gate_for_pr backend-tests 7 aaaa1111 && echo 0 || echo 1)"
echo '{"workflow_runs":[{"id":9,"name":"backend-tests","event":"pull_request","status":"in_progress","display_title":"x","head_sha":"aaaa1111"}]}' >"$SHIM_RUNS"
expect "a native run of the gate on the PR head is a running gate" "0" \
  "$(active_gate_for_pr backend-tests 7 aaaa1111 && echo 0 || echo 1)"
echo '{"workflow_runs":[{"id":9,"name":"backend-tests","event":"workflow_dispatch","status":"queued","display_title":"backend-tests [merge-ref #7]","head_sha":"bbbb2222"}]}' >"$SHIM_RUNS"
expect "a dispatched run for this PR is a running gate" "0" \
  "$(active_gate_for_pr backend-tests 7 aaaa1111 && echo 0 || echo 1)"
echo '{"workflow_runs":[{"id":9,"name":"backend-tests","event":"workflow_dispatch","status":"queued","display_title":"backend-tests [merge-ref #17]","head_sha":"bbbb2222"}]}' >"$SHIM_RUNS"
expect "a dispatched run for another PR is not" "1" \
  "$(active_gate_for_pr backend-tests 7 aaaa1111 && echo 0 || echo 1)"
echo '{"workflow_runs":[{"id":9,"name":"frontend-checks","event":"workflow_dispatch","status":"queued","display_title":"frontend-checks [merge-ref #7]","head_sha":"bbbb2222"}]}' >"$SHIM_RUNS"
expect "a dispatched run of another gate workflow is not" "1" \
  "$(active_gate_for_pr backend-tests 7 aaaa1111 && echo 0 || echo 1)"

# a stale PR is dispatched although the sweep's own job runs on main.
reset_shim
echo '{"workflow_runs":[{"id":9,"name":"Auto merge","event":"push","status":"in_progress","display_title":"sweep","head_sha":"bbbb2222"}]}' >"$SHIM_RUNS"
out="$(decide_pr "$PR7" 2>/dev/null)"
expect "a stale PR is dispatched while the sweep itself runs" "1" "$(dispatches)"

# a dispatched run for a PR is not the verdict about main.
GATE_WORKFLOW_LIST="backend-tests=integration"
reset_shim
echo '{"workflow_runs":[{"id":777,"name":"backend-tests","event":"workflow_dispatch","run_number":9,"display_title":"backend-tests [merge-ref #7]","head_sha":"bbbb2222"}]}' >"$SHIM_RUNS"
expect "a dispatched PR run on the main commit is not counted as main's gate" "green" \
  "$(main_gate_state 2>/dev/null)"

# 6. a dry run never dispatches and never comments.
reset_shim
DRY_RUN=true
out="$(decide_pr "$PR7" 2>"$pr_shim/stderr")"
expect "a dry run decides without dispatching" "skip|dry run: would dispatch the backend-tests gate on head aaaa1111 and main bbbb2222" "$out"
expect "a dry run starts no workflow" "0" "$(dispatches)"
refresh_ci_after_merges 2>"$pr_shim/stderr"
expect "a dry run refresh starts no workflow" "0" "$(dispatches)"
expect "a dry run refresh logs 'would'" "1" "$(grep -c 'dry-run: would dispatch' "$pr_shim/stderr")"
sweep_status_comment "$PR7" "some reason" 2>/dev/null
expect "a dry run writes no comment" "0" "$(grep -c 'comments' "$SHIM_LOG" || true)"
DRY_RUN=false

# 5. visibility: one log line per PR on stderr, one status comment per PR.
expect "log_decision writes to stderr" "" "$(log_decision 7 skip "why" 2>/dev/null)"
expect "log_decision names the PR, decision and detail" "1" \
  "$(log_decision 7 skip "why" 2>&1 >/dev/null | grep -c '#7 skip: why')"

reset_shim
sweep_status_comment "$PR7" "CI is red" 2>/dev/null
expect "a first skip creates the status comment" "1" "$(grep -c 'POST' "$SHIM_LOG")"
expect "the comment carries the hidden marker" "1" "$(grep -c 'automerge-sweep-status' "$SHIM_LOG")"

reset_shim
BODY="$(printf '%s\nThe CI auto-merge sweep skipped this pull request: %s.' '<!-- automerge-sweep-status -->' "CI is red")"
jq -cn --arg b "$BODY" '[{"id":42,"body":$b}]' >"$SHIM_COMMENTS"
sweep_status_comment "$PR7" "CI is red" 2>/dev/null
expect "the same reason writes nothing" "0" "$(wc -l <"$SHIM_LOG" | tr -d ' ')"
sweep_status_comment "$PR7" "conflict with main" 2>/dev/null
expect "a changed reason edits the one comment" "1" "$(grep -c 'PATCH.*issues/comments/42' "$SHIM_LOG")"
expect "a changed reason does not add a second comment" "0" "$(grep -c 'POST' "$SHIM_LOG")"

reset_shim
sweep_status_comment '{"number":7,"isDraft":true,"labels":[],"headRefOid":"aaaa1111"}' "draft" 2>/dev/null
expect "a draft gets no status comment" "0" "$(wc -l <"$SHIM_LOG" | tr -d ' ')"
rm -rf "$pr_shim"
unset SHIM_LOG SHIM_STATUSES SHIM_RUNS SHIM_COMMENTS GATE_WORKFLOW_LIST SHIM_MSTATE
PATH="${PATH#*:}"

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
