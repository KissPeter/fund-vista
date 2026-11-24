#!/usr/bin/env bash
#
# Decides whether a backend-tests run MUST run the suite, or MAY report its
# required jobs green without running it (no backend path changed).
#
# The workflow has no `paths` filter: the `main` ruleset requires the checks
# `integration (pytest)` and `schemathesis` on every pull request, and a
# workflow that a path filter skips reports no check at all, so a PR that
# does not touch the backend could never be merged (docs/auto-merge.md §3).
#
# Writes `backend=true|false` to $GITHUB_OUTPUT. The rule fails closed: every
# case it cannot decide runs the suite.
#
# Environment:
#   GITHUB_REPOSITORY  owner/repo
#   EVENT              github.event_name
#   PR_NUMBER          the PR (pull_request, or the dispatch input pr_number)
#   BEFORE, AFTER      the push range (push only)
#   GH_TOKEN           token for the API calls

set -euo pipefail

# The paths whose change MUST run the suite. Kept in sync with the jobs of
# .github/workflows/backend-tests.yml.
BACKEND_PATHS=(
  "backend/"
  "runtests.sh"
  "ops/schemathesis-smoke.sh"
  ".github/workflows/backend-tests.yml"
  ".github/scripts/backend-changes.sh"
)

# The compare API lists at most 300 files: a longer list is not trusted.
COMPARE_FILE_LIMIT=300

# is_backend_path <path> -> 0 when a change to that path MUST run the suite.
# Pure.
is_backend_path() {
  local path="$1" p
  for p in "${BACKEND_PATHS[@]}"; do
    case "$p" in
      */) [[ "$path" == "$p"* ]] && return 0 ;;
      *) [ "$path" = "$p" ] && return 0 ;;
    esac
  done
  return 1
}

# backend_changed <files (newline separated)> -> true | false. Pure.
backend_changed() {
  local file
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    if is_backend_path "$file"; then
      echo "true"
      return 0
    fi
  done <<<"$1"
  echo "false"
}

# decide <event> <pr number> <before> <files> <files read 0|1> <file count>
#   -> "true|false<TAB>reason". Pure: the file list is read by the caller.
decide() {
  local event="$1" pr="$2" before="$3" files="$4" read_ok="$5" count="$6"
  case "$event" in
    workflow_dispatch)
      if [ -z "$pr" ]; then
        printf 'true\ta manual run always runs the suite\n'
        return 0
      fi
      ;;
    pull_request) ;;
    push)
      if [ -z "$before" ] || [[ "$before" =~ ^0+$ ]]; then
        printf 'true\tthe push has no previous commit to compare with\n'
        return 0
      fi
      if [ "$count" -ge "$COMPARE_FILE_LIMIT" ]; then
        printf 'true\tthe push changed too many files to list\n'
        return 0
      fi
      ;;
    *)
      printf 'true\tunknown event %s\n' "$event"
      return 0
      ;;
  esac
  if [ "$read_ok" != 1 ]; then
    printf 'true\tthe changed files could not be read\n'
    return 0
  fi
  if [ "$(backend_changed "$files")" = "true" ]; then
    printf 'true\ta backend path changed\n'
  else
    printf 'false\tno backend path changed\n'
  fi
}

main() {
  local EVENT="${EVENT:-}"
  local files="" read_ok=0 count=0 result
  if [ -n "${PR_NUMBER:-}" ] && [ "$EVENT" != "push" ]; then
    if files="$(gh api --paginate "repos/$GITHUB_REPOSITORY/pulls/$PR_NUMBER/files?per_page=100" \
      --jq '.[].filename')"; then
      read_ok=1
    fi
  elif [ "$EVENT" = "push" ] && [ -n "${BEFORE:-}" ] && ! [[ "$BEFORE" =~ ^0+$ ]]; then
    if files="$(gh api "repos/$GITHUB_REPOSITORY/compare/$BEFORE...$AFTER" \
      --jq '.files[].filename')"; then
      read_ok=1
      count="$(grep -c . <<<"$files" || true)"
    fi
  fi
  result="$(decide "$EVENT" "${PR_NUMBER:-}" "${BEFORE:-}" "$files" "$read_ok" "$count")"
  echo "backend=${result%%$'\t'*}" >>"${GITHUB_OUTPUT:-/dev/null}"
  echo "run the suite: ${result%%$'\t'*} (${result#*$'\t'})"
}

# Sourcing the script (the self test does) must not run it.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
