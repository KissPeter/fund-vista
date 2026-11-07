#!/usr/bin/env bash
#
# Auto-merge sweep - merges the open pull requests that have no conflict
# with `main` and whose CI gate is green. The workflow that calls this script
# lives in .github/workflows/auto-merge.yml, the rules in docs/auto-merge.md.
#
# The script always runs from the BASE branch (the workflow checks out
# `main`), so a pull request can never change the rules that merge it.
#
# A PR is merged only when a gate run exists that
#   * belongs to the CURRENT head commit of the PR,
#   * tested the PR against the CURRENT `main` (a `pull_request` run whose
#     base SHA is the current main, or a dispatched run that merged the PR
#     head with the current main itself and reported `head <sha> main <sha>`),
#   * and whose blocking jobs are green.
# A green run against an older `main` does not count: the merge result would
# be unverified. GitHub's refs/pull/<n>/merge is NOT trusted: GitHub does not
# rebuild it when `main` moves. A red run against an older `main` does not
# block either (the `unstable` mergeable state is not a skip reason).
#
# Environment:
#   GITHUB_REPOSITORY        owner/repo (required)
#   GH_TOKEN                 token used for the API calls and for the merge
#   AUTOMERGE_TOKEN_SOURCE   "pat" (a PAT: its pushes trigger workflows)
#                             or "bot" (GITHUB_TOKEN: they do not)
#   AUTOMERGE_MERGE_METHOD   merge | squash | rebase        (default: merge)
#   AUTOMERGE_GATE_WORKFLOWS the gate workflows, comma separated:
#                             "workflow=job1+job2,other-workflow"
#                             (the job list is optional; default: CI=gate)
#                             A job name MAY contain spaces, e.g.
#                             "backend-tests=integration (pytest)+schemathesis"
#   AUTOMERGE_ADVISORY_CHECKS    jobs that never block a merge, comma
#                                 separated (default: live-e2e)
#   AUTOMERGE_BLOCK_LABELS   comma separated labels         (default:
#                             automerge:block,needs-human)
#   AUTOMERGE_BLOCKED_PATHS  paths a PR may not touch to be auto-merged
#                             (default: empty - the owner decides)
#   AUTOMERGE_REQUIRE_MAIN_GREEN true | false                (default: true)
#   AUTOMERGE_MAIN_WAIT_SECONDS  how long a pending gate on main is waited for
#                             before the sweep gives up   (default: 900)
#   AUTOMERGE_MAIN_POLL_SECONDS  pause between two checks of a pending main
#                             gate                          (default: 30)
#   AUTOMERGE_DISPATCH_CI    auto | never                   (default: auto)
#   AUTOMERGE_MAX_DISPATCH   merge-ref CI runs one sweep may start   (default: 3)
#                             A merge moves `main`, so every other PR must be
#                             re-verified; starting all of them at once floods
#                             the runners. The sweeps chain (each finished gate
#                             run triggers the next), so the rest follow.
#   AUTOMERGE_DRY_RUN        true | false                   (default: false)
#   CI_WORKFLOW              workflow file to re-dispatch   (default: ci.yml)

set -euo pipefail

REPO="${GITHUB_REPOSITORY:-}"
MERGE_METHOD="${AUTOMERGE_MERGE_METHOD:-merge}"
GATE_WORKFLOWS="${AUTOMERGE_GATE_WORKFLOWS:-CI=gate}"
ADVISORY_CHECKS="${AUTOMERGE_ADVISORY_CHECKS:-live-e2e}"
BLOCKED_PATHS="${AUTOMERGE_BLOCKED_PATHS:-}"
BLOCK_LABELS="${AUTOMERGE_BLOCK_LABELS:-automerge:block,needs-human}"
REQUIRE_MAIN_GREEN="${AUTOMERGE_REQUIRE_MAIN_GREEN:-true}"
MAIN_WAIT_SECONDS="${AUTOMERGE_MAIN_WAIT_SECONDS:-900}"
MAIN_POLL_SECONDS="${AUTOMERGE_MAIN_POLL_SECONDS:-30}"
DISPATCH_CI="${AUTOMERGE_DISPATCH_CI:-auto}"
MAX_DISPATCH="${AUTOMERGE_MAX_DISPATCH:-3}"
DRY_RUN="${AUTOMERGE_DRY_RUN:-false}"
TOKEN_SOURCE="${AUTOMERGE_TOKEN_SOURCE:-bot}"
CI_WORKFLOW="${CI_WORKFLOW:-ci.yml}"

# The gate entries, one per line: "workflow=job1+job2". Newline separated,
# because a job name may contain spaces ("integration (pytest)"). Iterate them
# with `while IFS= read -r entry <&3; do ...; done 3<<<"$GATE_WORKFLOW_LIST"`:
# fd 3 keeps the loop body's own stdin untouched.
GATE_WORKFLOW_LIST="$(echo "$GATE_WORKFLOWS" | tr ',' '\n')"

validate_config() {
  if [ -z "$REPO" ]; then
    echo "GITHUB_REPOSITORY is required" >&2
    exit 2
  fi
  case "$MERGE_METHOD" in
    merge | squash | rebase) ;;
    *)
      echo "AUTOMERGE_MERGE_METHOD must be merge, squash or rebase" >&2
      exit 2
      ;;
  esac
}

SUMMARY_ROWS=""
MERGED_COUNT=0
# The PRs this sweep already dispatched a gate for, " <n> <n> ". decide_pr
# runs in a subshell, so the main loop records them from the decision. The
# refresh pass skips them: the run it just dispatched is not listed by the
# API yet, and a second dispatch would occupy the runner twice.
DISPATCHED_PRS=" "
SKIPPED_COUNT=0
FAILED_COUNT=0
MAIN_SHA=""

# log and fail write to stderr: several helpers run inside $(...) and their
# result is read from stdout, so a log line there would corrupt the result.
log() {
  printf '%s %s\n' "$(date -u '+%H:%M:%S')" "$*" >&2
}

# fail <message> - a broken sweep must turn the job red: a sweep that could
# not do its work is not a sweep that found nothing to merge.
fail() {
  printf '::error::%s\n' "$*" >&2
  exit 1
}

# preflight - the tools and the API access the sweep cannot work without.
# Without it a missing `gh` or a bad token is swallowed by the `|| true` and
# `2>/dev/null` of the helpers below and the job ends green with nothing done.
preflight() {
  command -v gh >/dev/null 2>&1 || fail "gh is not installed on this runner"
  command -v jq >/dev/null 2>&1 || fail "jq is not installed on this runner"
  gh api "repos/$REPO" --jq '.full_name' >/dev/null 2>&1 \
    || fail "the GitHub API is not reachable or GH_TOKEN cannot read $REPO"
}

# add_row <pr> <title> <decision> <detail>
add_row() {
  SUMMARY_ROWS="${SUMMARY_ROWS}| ${1} | ${2} | ${3} | ${4} |
"
  case "$3" in
    merged | would-merge) MERGED_COUNT=$((MERGED_COUNT + 1)) ;;
    failed) FAILED_COUNT=$((FAILED_COUNT + 1)) ;;
    *) SKIPPED_COUNT=$((SKIPPED_COUNT + 1)) ;;
  esac
}

write_summary() {
  {
    echo "### Auto-merge sweep"
    echo ""
    echo "- repository: \`${REPO}\`"
    echo "- gate workflows: \`${GATE_WORKFLOWS}\`"
    echo "- merge method: \`${MERGE_METHOD}\`"
    echo "- token source: \`${TOKEN_SOURCE}\`"
    echo "- dry run: \`${DRY_RUN}\`"
    echo "- merged: ${MERGED_COUNT}, skipped: ${SKIPPED_COUNT}, failed: ${FAILED_COUNT}"
    echo ""
    echo "| PR | title | decision | detail |"
    echo "| --- | --- | --- | --- |"
    printf '%s' "$SUMMARY_ROWS"
  } >>"${GITHUB_STEP_SUMMARY:-/dev/null}"
}

# gate_workflow_name <entry> -> the workflow name
gate_workflow_name() {
  echo "${1%%=*}"
}

# gate_workflow_jobs <entry> -> the required job names, one per line (a job
# name may contain spaces)
gate_workflow_jobs() {
  local entry="$1" jobs="${1#*=}"
  if [ "$jobs" = "$entry" ]; then
    jobs=""
  fi
  echo "$jobs" | tr '+' '\n'
}

# has_block_label <labels (newline separated)> -> 0 when a block label is set
has_block_label() {
  local labels="$1" label
  for label in $(echo "$BLOCK_LABELS" | tr ',' ' '); do
    if echo "$labels" | grep -qx -- "$label"; then
      return 0
    fi
  done
  return 1
}

refresh_main_sha() {
  MAIN_SHA="$(gh api "repos/$REPO/commits/main" --jq '.sha' 2>/dev/null || true)"
}

# pr_merge_state <number> -> "<mergeable>|<mergeable_state>"
pr_merge_state() {
  local number="$1" attempt=0 out=""
  while [ "$attempt" -lt 4 ]; do
    out="$(gh api "repos/$REPO/pulls/$number" \
      --jq '[(.mergeable | tostring), (.mergeable_state // "unknown")] | join("|")' 2>/dev/null || true)"
    case "$out" in
      null\|*) sleep 5 ;;
      *) echo "$out"; return 0 ;;
    esac
    attempt=$((attempt + 1))
  done
  echo "unknown|unknown"
}

# merge_ref_sha <number> -> SHA of refs/pull/<number>/merge, empty when absent
merge_ref_sha() {
  gh api "repos/$REPO/git/ref/pull/$1/merge" --jq '.object.sha' 2>/dev/null || true
}

# merge_ref_is_current <merge ref sha> -> 0 when the merge ref was built on
# the current `main`. GitHub builds the merge ref from the base as it was at
# the build and does not rebuild it when `main` moves, so a ref built on an
# older `main` tests the PR against code that is gone. Fails closed: an
# unreadable parent counts as stale.
merge_ref_is_current() {
  local ref_sha="$1" parent
  [ -n "$ref_sha" ] && [ -n "$MAIN_SHA" ] || return 1
  parent="$(gh api "repos/$REPO/commits/$ref_sha" --jq '.parents[0].sha' 2>/dev/null || true)"
  [ -n "$parent" ] && [ "$parent" = "$MAIN_SHA" ]
}

# classify_jobs <jobs tsv> <required jobs> <advisory jobs>
# -> green | pending | red | missing
# Pure: the tsv is "name<TAB>status<TAB>conclusion" per line; the required and
# the advisory jobs are one name per line (a name may contain spaces).
# A required job is green only with the conclusion `success`. A gate job that
# found nothing to test (backend-tests on a PR without a backend change) skips
# its suite inside the job and still concludes `success`, so it is green. A
# required job concluded `skipped` is NOT green: the job did not run at all
# (fail closed).
classify_jobs() {
  local jobs="$1" required="$2" advisory="$3" name last blocking
  if [ -z "$jobs" ]; then
    echo "missing"
    return 0
  fi
  # A job that still runs means the verdict is not in yet - unless it is an
  # advisory job: it never blocks a merge, so it must not hold the sweep
  # back while it runs.
  # The lists reach awk through the environment: a `-v` value with a newline
  # is an error in BSD awk.
  if ADVS="$advisory" awk -F'\t' '
    BEGIN { m = split(ENVIRON["ADVS"], a, "\n") }
    {
      isadv = 0; for (i = 1; i <= m; i++) if (a[i] == $1) isadv = 1
      if (!isadv && $2 != "completed") found = 1
    }
    END { exit found ? 0 : 1 }' <<<"$jobs"; then
    echo "pending"
    return 0
  fi
  while IFS= read -r name <&3; do
    [ -n "$name" ] || continue
    last="$(awk -F'\t' -v n="$name" '$1 == n { c = $3 } END { print c }' <<<"$jobs")"
    if [ -z "$last" ]; then
      echo "missing"
      return 0
    fi
    if [ "$last" != "success" ]; then
      echo "red"
      return 0
    fi
  done 3<<<"$required"
  blocking="$(REQS="$required" ADVS="$advisory" awk -F'\t' '
    BEGIN { n = split(ENVIRON["REQS"], r, "\n"); m = split(ENVIRON["ADVS"], a, "\n") }
    {
      isreq = 0; for (i = 1; i <= n; i++) if (r[i] == $1) isreq = 1
      isadv = 0; for (i = 1; i <= m; i++) if (a[i] == $1) isadv = 1
      if (!isreq && !isadv && $3 != "success" && $3 != "skipped" && $3 != "neutral") print $1 "=" $3
    }' <<<"$jobs")"
  if [ -n "$blocking" ]; then
    echo "red"
    return 0
  fi
  echo "green"
}

# ci_run_jobs_state <run_id> <required jobs> -> green | pending | red | missing
ci_run_jobs_state() {
  local run_id="$1" required="$2" jobs
  jobs="$(gh api "repos/$REPO/actions/runs/$run_id/jobs?per_page=100" \
    --jq '.jobs[] | [.name, .status, (.conclusion // "none")] | @tsv' 2>/dev/null || true)"
  classify_jobs "$jobs" "$required" "$(echo "$ADVISORY_CHECKS" | tr ',' '\n')"
}

# fresh_ci_run <workflow> <head_sha> <merge_sha> -> the id of the newest gate
# run that tested the current head against the current main, empty when
# there is none
fresh_ci_run() {
  local workflow="$1" head_sha="$2" merge_sha="$3" runs merge_runs
  runs="$(gh api "repos/$REPO/actions/runs?head_sha=$head_sha&per_page=50" 2>/dev/null || echo '{"workflow_runs":[]}')"
  merge_runs='{"workflow_runs":[]}'
  if [ -n "$merge_sha" ]; then
    merge_runs="$(gh api "repos/$REPO/actions/runs?head_sha=$merge_sha&per_page=50" 2>/dev/null || echo '{"workflow_runs":[]}')"
  fi
  jq -n --argjson a "$runs" --argjson b "$merge_runs" \
    --arg main "$MAIN_SHA" --arg merge "$merge_sha" --arg head "$head_sha" --arg wf "$workflow" '
    [ ($a.workflow_runs + $b.workflow_runs)[]
      | select(.name == $wf)
      | select(
          ( .event == "pull_request"
            and (.pull_requests[0].head.sha // "") == $head
            and (.pull_requests[0].base.sha // "") == $main )
          or ( .event == "workflow_dispatch" and $merge != "" and .head_sha == $merge )
        )
    ] | sort_by(.run_number) | last | .id // empty'
}

# parse_merge_ref_status <statuses json> <context> -> "<state><TAB><description>"
# of the last status with that context, empty when there is none. Pure.
parse_merge_ref_status() {
  jq -r --arg ctx "${2:-automerge/merge-ref}" '
    ([.statuses[]? | select(.context == $ctx)] | sort_by(.created_at) | last)
    | if . == null then ""
      else ((.state // "unknown") + "\t" + (.description // ""))
      end' <<<"$1" 2>/dev/null || true
}

# merge_ref_status <head_sha> <context> -> the `automerge/merge-ref` status
# of that commit. The status is written by the gate workflow when the sweep
# dispatches it for a PR (a push with the GITHUB_TOKEN triggers no workflow
# run, so the sweep has to ask for the run itself; docs/auto-merge.md).
merge_ref_status() {
  local head_sha="$1" context="${2:-automerge/merge-ref}"
  parse_merge_ref_status "$(gh api "repos/$REPO/commits/$head_sha/status?per_page=100" 2>/dev/null || echo '{}')" "$context"
}

# merge_ref_verdict <status> <head sha> <main sha> <merge ref sha> <ref current 0|1>
#   -> green | stale | red | none
# Pure. `status` is "<state><TAB><description>" (see parse_merge_ref_status).
# The verdict is about the commit the run actually tested: the current form
# `head <head> main <main>` (the gate workflow merges the PR head with main).
# The old form `merge ref <sha>` counts only when that GitHub merge ref is
# built on the current main. A verdict about another head or main is stale: it
# asks for a new run, it never merges. Red counts only for the current head and
# main, so an old red verdict does not hold the gate back.
merge_ref_verdict() {
  local status="$1" head_sha="$2" main_sha="$3" ref_sha="$4" ref_current="$5"
  local state desc current=0
  local re_head='^head ([0-9a-f]+) main ([0-9a-f]+)$' re_ref='^merge ref ([0-9a-f]+)$'
  [ -n "$status" ] || { echo "none"; return 0; }
  state="${status%%$'\t'*}"
  desc="${status#*$'\t'}"
  if [[ "$desc" =~ $re_head ]]; then
    if [ "${BASH_REMATCH[1]}" = "$head_sha" ] && [ "${BASH_REMATCH[2]}" = "$main_sha" ]; then
      current=1
    fi
  elif [[ "$desc" =~ $re_ref ]]; then
    if [ "$ref_current" = "1" ] && [ "${BASH_REMATCH[1]}" = "$ref_sha" ]; then
      current=1
    fi
  else
    echo "stale"
    return 0
  fi
  case "$state" in
    success) if [ "$current" = 1 ]; then echo "green"; else echo "stale"; fi ;;
    failure | error) if [ "$current" = 1 ]; then echo "red"; else echo "stale"; fi ;;
    *) echo "none" ;;
  esac
}

# pr_touches_blocked_path <number> -> 0 when the PR changes a protected path,
# 1 when it does not, 2 when the changed files cannot be read. A PR whose file
# list is unreadable is skipped by the callers, never merged (fail closed).
# With an empty blocked-path list there is nothing to read, so the call is
# answered without touching `gh`.
pr_touches_blocked_path() {
  local number="$1" path files
  if [ -z "$BLOCKED_PATHS" ]; then
    return 1
  fi
  if ! files="$(gh pr view "$number" --repo "$REPO" --json files --jq '.files[].path' 2>/dev/null)"; then
    return 2
  fi
  local IFS=','
  for path in $BLOCKED_PATHS; do
    path="$(echo "$path" | tr -d '[:space:]')"
    [ -n "$path" ] || continue
    if echo "$files" | grep -q "^$path"; then
      return 0
    fi
  done
  return 1
}

# main_gate_state -> green | pending | red, the CI gate on the current main
# commit. Every gate workflow that ran on this main commit must be green. A
# gate workflow that did not run on it (path filter: a docs-only merge) is not
# counted, and a main commit without any gate run is not a blocker: nothing
# is known to be broken there. A red gate wins over a pending one.
main_gate_state() {
  local entry workflow run_id state verdict="green"
  refresh_main_sha
  if [ -z "$MAIN_SHA" ]; then
    # An unreadable `main` is a broken sweep, not a red `main`.
    fail "could not read the head of main"
  fi
  while IFS= read -r entry <&3; do
    [ -n "$entry" ] || continue
    workflow="$(gate_workflow_name "$entry")"
    run_id="$(gh api "repos/$REPO/actions/runs?head_sha=$MAIN_SHA&per_page=50" 2>/dev/null \
      | jq -r --arg wf "$workflow" \
        '[.workflow_runs[] | select(.name == $wf) | select((.display_title // "") | contains("[merge-ref #") | not)] | sort_by(.run_number) | last | .id // empty')"
    [ -n "$run_id" ] || continue
    state="$(ci_run_jobs_state "$run_id" "$(gate_workflow_jobs "$entry")")"
    log "main gate $workflow ($MAIN_SHA, run $run_id): $state"
    case "$state" in
      green) ;;
      pending) verdict="pending" ;;
      *) echo "red"; return 0 ;;
    esac
  done 3<<<"$GATE_WORKFLOW_LIST"
  echo "$verdict"
}

# wait_for_main -> green | red | pending. Polls the main gate while it is
# pending, up to MAIN_WAIT_SECONDS. `pending` as the result means the wait ran
# out. A new main commit during the wait is checked as well: the rule is about
# the commit that is current when the merge happens.
wait_for_main() {
  local waited=0 state
  while :; do
    state="$(main_gate_state)"
    if [ "$state" != "pending" ] || [ "$waited" -ge "$MAIN_WAIT_SECONDS" ]; then
      echo "$state"
      return 0
    fi
    log "main gate still pending: checking again in ${MAIN_POLL_SECONDS}s (waited ${waited}s of ${MAIN_WAIT_SECONDS}s)"
    sleep "$MAIN_POLL_SECONDS"
    waited=$((waited + MAIN_POLL_SECONDS))
  done
}

# active_gate_for_pr <workflow> <number> <head sha> -> 0 when a gate run of
# that workflow for the PR is queued or running: a native run on the PR head,
# or a dispatched `<workflow> [merge-ref #<n>]` run. Only the gate workflow
# counts. Any run on the `main` commit used to count, and the sweep's own job
# always runs there, so a PR whose verdict went stale was never dispatched
# again.
active_gate_for_pr() {
  local workflow="$1" number="$2" head_sha="$3" st n
  for st in in_progress queued; do
    n="$(gh api "repos/$REPO/actions/runs?head_sha=$head_sha&status=$st&per_page=100" \
      --jq "[.workflow_runs[] | select(.name == \"$workflow\" and .head_sha == \"$head_sha\")] | length" 2>/dev/null || echo 0)"
    [ "${n:-0}" != "0" ] && return 0
    n="$(gh api "repos/$REPO/actions/runs?event=workflow_dispatch&status=$st&per_page=100" \
      --jq "[.workflow_runs[] | select(.name == \"$workflow\" and ((.display_title // \"\") | endswith(\"[merge-ref #$number]\")))] | length" 2>/dev/null || echo 0)"
    [ "${n:-0}" != "0" ] && return 0
  done
  return 1
}

# dispatch_ci <workflow> <ref> -> 0 when the workflow was dispatched for that
# ref. A dry run only logs what it would do.
dispatch_ci() {
  local workflow="$1" ref="$2"
  if [ "$DRY_RUN" = "true" ]; then
    log "dry-run: would dispatch $workflow for $ref"
    return 0
  fi
  if gh workflow run "$workflow" --repo "$REPO" --ref "$ref" >/dev/null 2>&1; then
    log "dispatched $workflow for $ref"
    return 0
  fi
  log "WARNING: could not dispatch $workflow for $ref"
  return 1
}

# dispatch_ci_for_pr <workflow> <number> <main sha> -> 0 when the gate was
# dispatched for the PR head merged with that main commit. The run does not
# depend on GitHub's merge ref, which is not rebuilt when `main` moves: the
# gate workflow checks out the PR head, merges <main sha> into it, and reports
# the verdict as the commit status `automerge/merge-ref/<workflow>` on the PR
# head, described `head <head> main <main>`. The run itself belongs to `main`.
# A dry run only logs what it would do.
dispatch_ci_for_pr() {
  local workflow="$1" number="$2" main="$3"
  if [ -z "$main" ]; then
    log "WARNING: no main commit to dispatch PR #$number against"
    return 1
  fi
  if [ "$DRY_RUN" = "true" ]; then
    log "dry-run: would dispatch $workflow for PR #$number on main $main"
    return 0
  fi
  if gh workflow run "$workflow" --repo "$REPO" --ref main \
    -f "pr_number=$number" -f "main_sha=$main" >/dev/null 2>&1; then
    log "dispatched $workflow for PR #$number on main $main"
    return 0
  fi
  log "WARNING: could not dispatch $workflow for PR #$number"
  return 1
}

list_open_prs() {
  gh pr list --repo "$REPO" --state open --base main --limit 100 \
    --json number,title,isDraft,labels,headRefName,headRefOid,author,url
}

# decide_pr <pr-json> -> "<decision>|<detail>"
decide_pr() {
  local pr="$1"
  local number draft labels state mergeable merge_state head_sha ref_sha legacy
  local entry workflow required run_id job_state verdict gate_detail="" gate_found=0
  local ref_current tested_ref blocked_status=0
  number="$(jq -r '.number' <<<"$pr")"
  draft="$(jq -r '.isDraft' <<<"$pr")"
  labels="$(jq -r '[.labels[].name] | join("\n")' <<<"$pr")"
  head_sha="$(jq -r '.headRefOid' <<<"$pr")"

  if [ "$draft" = "true" ]; then
    echo "skip|draft"
    return 0
  fi
  if has_block_label "$labels"; then
    echo "skip|block label"
    return 0
  fi
  # Fail closed: an unreadable file list must not look like "touches no
  # blocked path". Statuses: 0 = touches, 1 = clean, 2 = unread. The initial
  # value must be 0: `cmd || var=$?` only assigns on failure.
  pr_touches_blocked_path "$number" || blocked_status=$?
  case "$blocked_status" in
    0)
      echo "skip|the PR changes a protected path"
      return 0
      ;;
    2)
      echo "skip|could not read the PR's changed files"
      return 0
      ;;
  esac

  state="$(pr_merge_state "$number")"
  mergeable="${state%%|*}"
  merge_state="${state##*|}"
  case "$mergeable" in
    false)
      echo "skip|conflict with main"
      return 0
      ;;
    unknown)
      echo "skip|mergeability unknown (GitHub is still computing it)"
      return 0
      ;;
  esac
  case "$merge_state" in
    dirty)
      echo "skip|conflict with main"
      return 0
      ;;
    blocked)
      echo "skip|blocked by the repository rules"
      return 0
      ;;
    # `unstable` (a check of the head is red) is deliberately not a skip. A
    # red native run of the head may have tested an older `main`, and it
    # decides nothing about the current merge: the gate verdict below decides,
    # not this field.
  esac

  ref_sha="$(merge_ref_sha "$number")"
  if [ -z "$ref_sha" ]; then
    echo "skip|no merge ref (the branch conflicts with main)"
    return 0
  fi

  # GitHub's merge ref is not rebuilt when `main` moves, so it is trusted only
  # when it is built on the current main. Nothing else depends on it: the
  # dispatched gate merges the PR head with the current main itself.
  if [ -z "$MAIN_SHA" ]; then
    echo "skip|could not read the head of main"
    return 0
  fi
  ref_current=0
  if merge_ref_is_current "$ref_sha"; then
    ref_current=1
  fi
  tested_ref=""
  if [ "$ref_current" = 1 ]; then tested_ref="$ref_sha"; fi

  # The gate workflows, one by one. A workflow with no run for this PR did
  # not run (path filter) and is not counted. At least one gate workflow
  # must have run and be green: a PR without any check is not a pass.
  while IFS= read -r entry <&3; do
    [ -n "$entry" ] || continue
    workflow="$(gate_workflow_name "$entry")"
    required="$(gate_workflow_jobs "$entry")"
    run_id="$(fresh_ci_run "$workflow" "$head_sha" "$tested_ref")"
    if [ -z "$run_id" ]; then
      # No native run against the current main (a green run on the head
      # against an older main does not count). The sweep dispatches the gate
      # for the current merge ref itself and merges on a later sweep, once
      # the verdict is in.
      verdict="$(merge_ref_verdict "$(merge_ref_status "$head_sha" "automerge/merge-ref/$workflow")" \
        "$head_sha" "$MAIN_SHA" "$ref_sha" "$ref_current")"
      case "$verdict" in
        green)
          gate_found=1
          gate_detail="the dispatched $workflow gate is green on head $head_sha and main $MAIN_SHA"
          ;;
        none | stale)
          if active_gate_for_pr "$workflow" "$number" "$head_sha"; then
            echo "skip|the $workflow gate is still running"
            return 0
          fi
          if [ "$DRY_RUN" = "true" ]; then
            echo "skip|dry run: would dispatch the $workflow gate on head $head_sha and main $MAIN_SHA"
            return 0
          fi
          dispatch_ci_for_pr "$workflow" "$number" "$MAIN_SHA" || true
          echo "skip|dispatched the $workflow gate on head $head_sha and main $MAIN_SHA; a later sweep merges"
          return 0
          ;;
        red)
          echo "skip|the dispatched $workflow gate is red"
          return 0
          ;;
      esac
      continue
    fi
    job_state="$(ci_run_jobs_state "$run_id" "$required")"
    case "$job_state" in
      green) gate_found=1 ;;
      pending)
        echo "skip|the $workflow gate is still running"
        return 0
        ;;
      red)
        echo "skip|the $workflow gate is red"
        return 0
        ;;
      *)
        echo "skip|the $workflow gate has no verdict"
        return 0
        ;;
    esac
  done 3<<<"$GATE_WORKFLOW_LIST"
  if [ "$gate_found" -eq 0 ]; then
    echo "skip|no CI gate ran for this pull request"
    return 0
  fi

  legacy="$(gh api "repos/$REPO/commits/$head_sha/status" 2>/dev/null \
    | jq -r 'if (.total_count // 0) == 0 then "none" else (.state // "unknown") end')"
  case "$legacy" in
    none | success) ;;
    *)
      echo "skip|legacy commit status is $legacy"
      return 0
      ;;
  esac

  # Re-verify right before the merge: the head or the base may have moved
  # while the sweep was running.
  local current_head current_main
  current_head="$(gh api "repos/$REPO/pulls/$number" --jq '.head.sha' 2>/dev/null || true)"
  current_main="$(gh api "repos/$REPO/commits/main" --jq '.sha' 2>/dev/null || true)"
  if [ "$current_head" != "$head_sha" ] || [ "$current_main" != "$MAIN_SHA" ]; then
    echo "skip|the PR moved while the sweep ran"
    return 0
  fi

  echo "merge|$gate_detail"
}

merge_pr() {
  local number="$1"
  if [ "$DRY_RUN" = "true" ]; then
    log "dry-run: would merge #$number"
    return 0
  fi
  local err
  if err="$(gh pr merge "$number" --repo "$REPO" --"$MERGE_METHOD" 2>&1 >/dev/null)"; then
    log "merged #$number"
    cleanup_issue_labels "$number"
    return 0
  fi
  log "WARNING: the merge of #$number failed: ${err:-no output from gh}"
  return 1
}

# cleanup_issue_labels drops the agent labels from the issue the merged PR
# closes. The PR closes the issue through `Closes #<n>`; the labels are the
# orchestrator's bookkeeping and MUST NOT outlive the merge.
cleanup_issue_labels() {
  local number="$1" body issue
  body="$(gh pr view "$number" --repo "$REPO" --json body --jq '.body' 2>/dev/null || true)"
  [ -n "$body" ] || return 0
  while IFS= read -r issue; do
    [ -n "$issue" ] || continue
    gh issue edit "$issue" --repo "$REPO" \
      --remove-label "agent:in-progress" --remove-label "agent:needs-review" >/dev/null 2>&1 || true
    log "cleaned the agent labels of issue #$issue"
  done < <(grep -oiE '(closes|fixes|resolves)[[:space:]]+#[0-9]+' <<<"$body" | grep -oE '[0-9]+')
}

# refresh_ci_after_merges keeps the CI alive when the merge used the
# GITHUB_TOKEN: such a push triggers no workflow run at all. It also runs
# when nothing was merged: `main` may have moved without a sweep merge (a
# human push or merge), and then no PR has a run against the new `main`.
# It skips what decide_pr skips (drafts, block labels, protected paths, an
# unreadable file list, conflicts), so no gate run is spent on a PR that can
# never be merged.
refresh_ci_after_merges() {
  [ "$TOKEN_SOURCE" = "bot" ] || return 0
  [ "$DISPATCH_CI" = "never" ] && return 0

  local entry workflow
  if [ "$MERGED_COUNT" -gt 0 ]; then
    log "the merge used the GITHUB_TOKEN: re-dispatching the CI by hand"
    while IFS= read -r entry <&3; do
      [ -n "$entry" ] || continue
      workflow="$(gate_workflow_name "$entry")"
      dispatch_ci "$workflow" "main" || true
    done 3<<<"$GATE_WORKFLOW_LIST"
  fi

  local prs pr number head_sha ref_sha state mstate mergeable verdict labels blocked_status
  local ref_current tested_ref dispatched=0
  prs="$(list_open_prs || echo '[]')"
  while IFS= read -r pr; do
    [ -n "$pr" ] || continue
    number="$(jq -r '.number' <<<"$pr")"
    if [ "$(jq -r '.isDraft' <<<"$pr")" = "true" ]; then
      continue
    fi
    case "$DISPATCHED_PRS" in
      *" $number "*) continue ;;
    esac
    labels="$(jq -r '[.labels[].name] | join("\n")' <<<"$pr")"
    if has_block_label "$labels"; then
      continue
    fi
    blocked_status=0
    pr_touches_blocked_path "$number" || blocked_status=$?
    [ "$blocked_status" -eq 1 ] || continue
    mstate="$(pr_merge_state "$number")"
    mergeable="${mstate%%|*}"
    if [ "$mergeable" = "false" ] || [ "${mstate##*|}" = "dirty" ]; then
      continue
    fi
    head_sha="$(jq -r '.headRefOid' <<<"$pr")"
    ref_sha="$(merge_ref_sha "$number")"
    [ -n "$ref_sha" ] || continue
    ref_current=0
    if merge_ref_is_current "$ref_sha"; then
      ref_current=1
    fi
    tested_ref=""
    if [ "$ref_current" = 1 ]; then tested_ref="$ref_sha"; fi
    while IFS= read -r entry <&3; do
      [ -n "$entry" ] || continue
      workflow="$(gate_workflow_name "$entry")"
      state="$(ci_run_jobs_state "$(fresh_ci_run "$workflow" "$head_sha" "$tested_ref")" "$(gate_workflow_jobs "$entry")")"
      [ "$state" = "green" ] && continue
      verdict="$(merge_ref_verdict "$(merge_ref_status "$head_sha" "automerge/merge-ref/$workflow")" \
        "$head_sha" "$MAIN_SHA" "$ref_sha" "$ref_current")"
      case "$verdict" in
        # A green verdict on this head and main is waiting for the merge. A
        # red one is not re-dispatched by its own completion: the sweep wakes
        # on every gate completion and a dispatched gate IS a gate run, so a
        # red dispatch would re-dispatch itself until it went green. A fresh
        # verdict for a red PR comes from the other triggers (a `main` push,
        # the PR updated, a manual sweep), not from the failure itself.
        green | red) continue ;;
      esac
      if active_gate_for_pr "$workflow" "$number" "$head_sha"; then
        log "#$number already has a running $workflow run"
        continue
      fi
      if [ "$dispatched" -ge "$MAX_DISPATCH" ]; then
        log "dispatch limit ($MAX_DISPATCH) reached: #$number is verified by a later sweep"
        continue
      fi
      if dispatch_ci_for_pr "$workflow" "$number" "$MAIN_SHA"; then
        dispatched=$((dispatched + 1))
      fi
    done 3<<<"$GATE_WORKFLOW_LIST"
  done < <(jq -c '.[]' <<<"$prs")
}

# log_decision <pr number> <decision> <detail> -> one line per PR in the run
# log, so `gh run view --log` shows why a PR was merged or skipped.
log_decision() {
  log "#$1 $2: $3"
}

# sweep_status_comment <pr json> <reason> - keeps ONE comment per skipped PR,
# carrying the current skip reason. The comment is found by its hidden marker
# and edited only when the reason changed. Never in a dry run, never on a draft.
sweep_status_comment() {
  local pr="$1" reason="$2" number body existing id
  number="$(jq -r '.number' <<<"$pr")"
  [ "$DRY_RUN" = "true" ] && return 0
  [ "$(jq -r '.isDraft' <<<"$pr")" = "true" ] && return 0
  body="$(printf '%s\nThe CI auto-merge sweep skipped this pull request: %s.' \
    '<!-- automerge-sweep-status -->' "$reason")"
  existing="$(gh api "repos/$REPO/issues/$number/comments?per_page=100" --paginate \
    --jq '.[] | select((.body // "") | contains("<!-- automerge-sweep-status -->")) | tojson' \
    2>/dev/null | tail -n 1 || true)"
  if [ -z "$existing" ]; then
    gh api -X POST "repos/$REPO/issues/$number/comments" -f body="$body" >/dev/null 2>&1 \
      || log "WARNING: could not comment the skip reason on #$number"
    return 0
  fi
  id="$(jq -r '.id' <<<"$existing")"
  if [ "$(jq -r '.body' <<<"$existing")" = "$body" ]; then
    return 0
  fi
  gh api -X PATCH "repos/$REPO/issues/comments/$id" -f body="$body" >/dev/null 2>&1 \
    || log "WARNING: could not update the status comment on #$number"
}

main() {
  validate_config
  log "sweep start: repo=$REPO token=$TOKEN_SOURCE method=$MERGE_METHOD gates=$GATE_WORKFLOWS dry_run=$DRY_RUN"
  preflight

  if [ "$REQUIRE_MAIN_GREEN" = "true" ]; then
    main_state="$(main_gate_state)"
    if [ "$main_state" = "pending" ]; then
      log "main gate is pending: waiting up to ${MAIN_WAIT_SECONDS}s for it"
      main_state="$(wait_for_main)"
      if [ "$main_state" = "pending" ]; then
        printf '::notice::the sweep timed out waiting for main after %ss: the gate on main is still running, so nothing was merged; the next sweep merges\n' "$MAIN_WAIT_SECONDS"
      fi
    fi
    if [ "$main_state" != "green" ]; then
      log "main is not green ($main_state): no PR is merged in this run"
      printf '::warning::main is not green (%s): the sweep merged nothing\n' "$main_state"
      add_row "-" "-" "skipped" "main is not green ($main_state)"
      write_summary
      exit 0
    fi
  fi

  # The freshness checks compare against `main`, which the green check above
  # may not have read (REQUIRE_MAIN_GREEN=false).
  refresh_main_sha

  local prs pr number title decision detail
  prs="$(list_open_prs)"
  # Evaluate the PRs in parallel: each PR needs several independent API
  # calls, and the number of open PRs can be large. The verdicts are
  # written to one file per PR number; the merge itself stays serial (one
  # merge per cycle, because `main` moves after a merge).
  # Global on purpose: the EXIT trap runs after main() returned, when a local
  # would be out of scope and `set -u` would turn the cleanup into a failure.
  EVAL_DIR="$(mktemp -d)"
  trap 'rm -rf "${EVAL_DIR:-}"' EXIT
  local pids=()
  while IFS= read -r pr; do
    [ -n "$pr" ] || continue
    number="$(jq -r '.number' <<<"$pr")"
    (
      result="$(decide_pr "$pr")"
      printf '%s\n' "$result" >"$EVAL_DIR/$number"
    ) &
    pids+=($!)
    if [ "${#pids[@]}" -ge 8 ]; then
      wait "${pids[@]}" 2>/dev/null || true
      pids=()
    fi
  done < <(jq -c '.[]' <<<"$prs")
  [ "${#pids[@]}" -gt 0 ] && wait "${pids[@]}" 2>/dev/null || true

  while IFS= read -r pr; do
    [ -n "$pr" ] || continue
    number="$(jq -r '.number' <<<"$pr")"
    title="$(jq -r '.title' <<<"$pr")"
    # A PR without a verdict file means its evaluation crashed: that is a
    # failure of the sweep, not a PR that is merely not ready.
    decision="$(cat "$EVAL_DIR/$number" 2>/dev/null || echo "error|the evaluation produced no verdict")"
    detail="${decision#*|}"
    decision="${decision%%|*}"
    log_decision "$number" "$decision" "$detail"
    case "$detail" in
      "dispatched the "*) DISPATCHED_PRS="${DISPATCHED_PRS}${number} " ;;
    esac
    case "$decision" in
      merge)
        if merge_pr "$number"; then
          add_row "#$number" "$title" "$([ "$DRY_RUN" = "true" ] && echo would-merge || echo merged)" "$detail"
          if [ "$DRY_RUN" != "true" ]; then
            gh pr comment "$number" --repo "$REPO" --body \
              "Auto-merged by the CI sweep: the gate is green on the current \`main\` and the branch has no conflict with it." \
              >/dev/null 2>&1 || true
            # `main` moved: every other PR must be verified against the new one.
            refresh_main_sha
            break
          fi
        else
          add_row "#$number" "$title" "failed" "$detail"
        fi
        ;;
      error)
        add_row "#$number" "$title" "failed" "$detail"
        ;;
      *)
        add_row "#$number" "$title" "skipped" "$detail"
        sweep_status_comment "$pr" "$detail"
        ;;
    esac
  done < <(jq -c '.[]' <<<"$prs")

  refresh_ci_after_merges

  log "sweep done: merged=$MERGED_COUNT skipped=$SKIPPED_COUNT failed=$FAILED_COUNT"
  write_summary
  if [ "$FAILED_COUNT" -gt 0 ]; then
    fail "$FAILED_COUNT pull request(s) could not be processed (see the summary)"
  fi
}

# Sourcing the script (the self test does) must not run the sweep.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
