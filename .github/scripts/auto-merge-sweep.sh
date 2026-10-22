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
#     base SHA is the current main, or a run dispatched on the PR merge ref),
#   * and whose blocking jobs are green.
# A green run against an older `main` does not count: the merge result would
# be unverified.
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
#   AUTOMERGE_ADVISORY_CHECKS    jobs that never block a merge (default:
#                                 live-e2e)
#   AUTOMERGE_BLOCK_LABELS   comma separated labels         (default:
#                             automerge:block,needs-human)
#   AUTOMERGE_BLOCKED_PATHS  paths a PR may not touch to be auto-merged
#                             (default: empty - the owner decides)
#   AUTOMERGE_REQUIRE_MAIN_GREEN true | false                (default: true)
#   AUTOMERGE_DISPATCH_CI    auto | never                   (default: auto)
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
DISPATCH_CI="${AUTOMERGE_DISPATCH_CI:-auto}"
DRY_RUN="${AUTOMERGE_DRY_RUN:-false}"
TOKEN_SOURCE="${AUTOMERGE_TOKEN_SOURCE:-bot}"
CI_WORKFLOW="${CI_WORKFLOW:-ci.yml}"

# The gate entries, space separated: "workflow=job1+job2"
GATE_WORKFLOW_LIST="$(echo "$GATE_WORKFLOWS" | tr ',' ' ')"

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
SKIPPED_COUNT=0
FAILED_COUNT=0
MAIN_SHA=""

log() {
  printf '%s %s\n' "$(date -u '+%H:%M:%S')" "$*"
}

# fail <message> - a broken sweep must turn the job red: a sweep that could
# not do its work is not a sweep that found nothing to merge.
fail() {
  printf '::error::%s\n' "$*"
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

# gate_workflow_jobs <entry> -> the required job names, space separated
gate_workflow_jobs() {
  local entry="$1" jobs="${1#*=}"
  if [ "$jobs" = "$entry" ]; then
    jobs=""
  fi
  echo "$jobs" | tr '+' ' '
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

# classify_jobs <jobs tsv> <required jobs> <advisory jobs>
# -> green | pending | red | missing
# Pure: the tsv is "name<TAB>status<TAB>conclusion" per line.
classify_jobs() {
  local jobs="$1" required="$2" advisory="$3" name last blocking
  if [ -z "$jobs" ]; then
    echo "missing"
    return 0
  fi
  # A job that still runs means the verdict is not in yet - unless it is an
  # advisory job: it never blocks a merge, so it must not hold the sweep
  # back while it runs.
  if awk -F'\t' -v advs="$advisory" '
    BEGIN { m = split(advs, a, " ") }
    {
      isadv = 0; for (i = 1; i <= m; i++) if (a[i] == $1) isadv = 1
      if (!isadv && $2 != "completed") found = 1
    }
    END { exit found ? 0 : 1 }' <<<"$jobs"; then
    echo "pending"
    return 0
  fi
  for name in $required; do
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
  done
  blocking="$(awk -F'\t' -v reqs="$required" -v advs="$advisory" '
    BEGIN { n = split(reqs, r, " "); m = split(advs, a, " ") }
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
  classify_jobs "$jobs" "$required" "$ADVISORY_CHECKS"
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

# parse_merge_ref_status <statuses json> <context> -> "<state> <merge_sha>"
# of the last status with that context, empty when there is none. Pure.
parse_merge_ref_status() {
  jq -r --arg ctx "${2:-automerge/merge-ref}" '
    ([.statuses[]? | select(.context == $ctx)] | sort_by(.created_at) | last)
    | if . == null then ""
      else ((.state // "unknown") + " " + ((.description // "") | split(" ") | last))
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

# merge_ref_verdict <status> <current merge ref sha> -> green | stale | red | none
# Pure. `status` is "<state> <tested merge ref>".
merge_ref_verdict() {
  local status="$1" ref_sha="$2" state tested
  state="${status%% *}"
  tested="${status##* }"
  case "$state" in
    success)
      if [ "$tested" = "$ref_sha" ]; then
        echo "green"
      else
        echo "stale"
      fi
      ;;
    failure | error) echo "red" ;;
    *) echo "none" ;;
  esac
}

# pr_touches_blocked_path <number> -> 0 when the PR changes a protected path
pr_touches_blocked_path() {
  local number="$1" path
  for path in $(echo "$BLOCKED_PATHS" | tr ',' ' '); do
    if gh pr view "$number" --repo "$REPO" --json files --jq '.files[].path' 2>/dev/null \
      | grep -q "^$path"; then
      return 0
    fi
  done
  return 1
}

# main_is_green -> 0 when the CI gate on the current main commit is green.
# Every gate workflow that ran on this main commit must be green. A gate
# workflow that did not run on it (path filter: a docs-only merge) is not
# counted, and a main commit without any gate run is not a blocker: nothing
# is known to be broken there.
main_is_green() {
  local entry workflow run_id state
  refresh_main_sha
  if [ -z "$MAIN_SHA" ]; then
    # An unreadable `main` is a broken sweep, not a red `main`.
    fail "could not read the head of main"
  fi
  for entry in $GATE_WORKFLOW_LIST; do
    workflow="$(gate_workflow_name "$entry")"
    run_id="$(gh api "repos/$REPO/actions/runs?head_sha=$MAIN_SHA&per_page=50" 2>/dev/null \
      | jq -r --arg wf "$workflow" \
        '[.workflow_runs[] | select(.name == $wf)] | sort_by(.run_number) | last | .id // empty')"
    [ -n "$run_id" ] || continue
    state="$(ci_run_jobs_state "$run_id" "$(gate_workflow_jobs "$entry")")"
    log "main gate $workflow ($MAIN_SHA, run $run_id): $state"
    if [ "$state" != "green" ]; then
      return 1
    fi
  done
  return 0
}

# active_run_for_sha <sha> -> 0 when a CI run for that SHA is queued or running
active_run_for_sha() {
  local sha="$1" total
  [ -n "$sha" ] || return 1
  total="$(gh api "repos/$REPO/actions/runs?head_sha=$sha&status=in_progress&per_page=1" --jq '.total_count' 2>/dev/null || echo 0)"
  [ "$total" != "0" ] && return 0
  total="$(gh api "repos/$REPO/actions/runs?head_sha=$sha&status=queued&per_page=1" --jq '.total_count' 2>/dev/null || echo 0)"
  [ "$total" != "0" ]
}

# dispatch_ci <workflow> <ref> -> 0 when the workflow was dispatched for that ref
dispatch_ci() {
  local workflow="$1" ref="$2"
  if gh workflow run "$workflow" --repo "$REPO" --ref "$ref" >/dev/null 2>&1; then
    log "dispatched $workflow for $ref"
    return 0
  fi
  log "WARNING: could not dispatch $workflow for $ref"
  return 1
}

# dispatch_ci_for_pr <workflow> <number> -> 0 when the gate was dispatched for
# the merge ref of that PR. The run reports its verdict as the commit status
# `automerge/merge-ref/<workflow>` on the head commit of the PR, because the
# run itself belongs to `main` (a dispatch cannot target a PR merge ref: the
# API answers "No ref found for: refs/pull/<n>/merge").
dispatch_ci_for_pr() {
  local workflow="$1" number="$2"
  if gh workflow run "$workflow" --repo "$REPO" --ref main -f "pr_number=$number" >/dev/null 2>&1; then
    log "dispatched $workflow for the merge ref of PR #$number"
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
  if pr_touches_blocked_path "$number"; then
    echo "skip|the PR changes a protected path"
    return 0
  fi

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
    unstable)
      echo "skip|a check is failing"
      return 0
      ;;
  esac

  ref_sha="$(merge_ref_sha "$number")"
  if [ -z "$ref_sha" ]; then
    echo "skip|no merge ref (the branch conflicts with main)"
    return 0
  fi

  # The gate workflows, one by one. A workflow with no run for this PR did
  # not run (path filter) and is not counted. At least one gate workflow
  # must have run and be green: a PR without any check is not a pass.
  for entry in $GATE_WORKFLOW_LIST; do
    workflow="$(gate_workflow_name "$entry")"
    required="$(gate_workflow_jobs "$entry")"
    run_id="$(fresh_ci_run "$workflow" "$head_sha" "$ref_sha")"
    if [ -z "$run_id" ]; then
      # No native run against the current main. With a PAT the runs come
      # back on their own; with the GITHUB_TOKEN the sweep asks for them
      # (dispatch_ci_for_pr) and reads the verdict from the commit status.
      verdict="$(merge_ref_verdict "$(merge_ref_status "$head_sha" "automerge/merge-ref/$workflow")" "$ref_sha")"
      case "$verdict" in
        green)
          gate_found=1
          gate_detail="the dispatched $workflow gate is green on the current merge ref"
          ;;
        stale)
          echo "skip|the dispatched $workflow gate tested another merge ref"
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
  done
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
  local current_head current_ref
  current_head="$(gh api "repos/$REPO/pulls/$number" --jq '.head.sha' 2>/dev/null || true)"
  current_ref="$(merge_ref_sha "$number")"
  if [ "$current_head" != "$head_sha" ] || [ "$current_ref" != "$ref_sha" ]; then
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
  if gh pr merge "$number" --repo "$REPO" --"$MERGE_METHOD" >/dev/null 2>&1; then
    log "merged #$number"
    cleanup_issue_labels "$number"
    return 0
  fi
  log "WARNING: the merge of #$number failed"
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
# GITHUB_TOKEN: such a push triggers no workflow run at all.
refresh_ci_after_merges() {
  [ "$TOKEN_SOURCE" = "bot" ] || return 0
  [ "$DISPATCH_CI" = "never" ] && return 0
  [ "$MERGED_COUNT" -gt 0 ] || return 0

  log "the merge used the GITHUB_TOKEN: re-dispatching the CI by hand"
  local entry workflow
  for entry in $GATE_WORKFLOW_LIST; do
    workflow="$(gate_workflow_name "$entry")"
    dispatch_ci "$workflow" "main" || true
  done

  local prs pr number head_sha ref_sha state mstate mergeable verdict
  prs="$(list_open_prs || echo '[]')"
  while IFS= read -r pr; do
    [ -n "$pr" ] || continue
    number="$(jq -r '.number' <<<"$pr")"
    if [ "$(jq -r '.isDraft' <<<"$pr")" = "true" ]; then
      continue
    fi
    mstate="$(pr_merge_state "$number")"
    mergeable="${mstate%%|*}"
    if [ "$mergeable" = "false" ] || [ "${mstate##*|}" = "dirty" ]; then
      continue
    fi
    head_sha="$(jq -r '.headRefOid' <<<"$pr")"
    ref_sha="$(merge_ref_sha "$number")"
    [ -n "$ref_sha" ] || continue
    for entry in $GATE_WORKFLOW_LIST; do
      workflow="$(gate_workflow_name "$entry")"
      state="$(ci_run_jobs_state "$(fresh_ci_run "$workflow" "$head_sha" "$ref_sha")" "$(gate_workflow_jobs "$entry")")"
      [ "$state" = "green" ] && continue
      verdict="$(merge_ref_verdict "$(merge_ref_status "$head_sha" "automerge/merge-ref/$workflow")" "$ref_sha")"
      if [ "$verdict" = "green" ]; then
        continue
      fi
      if active_run_for_sha "$ref_sha" || active_run_for_sha "$head_sha"; then
        log "#$number already has a running CI run"
        continue
      fi
      dispatch_ci_for_pr "$workflow" "$number" || true
    done
  done < <(jq -c '.[]' <<<"$prs")
}

main() {
  validate_config
  log "sweep start: repo=$REPO token=$TOKEN_SOURCE method=$MERGE_METHOD gates=$GATE_WORKFLOWS dry_run=$DRY_RUN"
  preflight

  if [ "$REQUIRE_MAIN_GREEN" = "true" ] && ! main_is_green; then
    log "main is not green: no PR is merged in this run"
    printf '::warning::main is not green: the sweep merged nothing\n'
    add_row "-" "-" "skipped" "main is not green"
    write_summary
    exit 0
  fi

  local prs pr number title decision detail
  prs="$(list_open_prs)"
  # Evaluate the PRs in parallel: each PR needs several independent API
  # calls, and the number of open PRs can be large. The verdicts are
  # written to one file per PR number; the merge itself stays serial (one
  # merge per cycle, because `main` moves after a merge).
  local eval_dir
  eval_dir="$(mktemp -d)"
  trap 'rm -rf "$eval_dir"' EXIT
  local pids=()
  while IFS= read -r pr; do
    [ -n "$pr" ] || continue
    number="$(jq -r '.number' <<<"$pr")"
    (
      result="$(decide_pr "$pr")"
      printf '%s\n' "$result" >"$eval_dir/$number"
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
    decision="$(cat "$eval_dir/$number" 2>/dev/null || echo "error|the evaluation produced no verdict")"
    detail="${decision#*|}"
    decision="${decision%%|*}"
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
