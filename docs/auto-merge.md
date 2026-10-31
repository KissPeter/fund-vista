# Auto-merge (the CI sweep)

Status: adopted 2026-10-22 (owner decision).

The key words MUST, MUST NOT, REQUIRED, SHALL, SHALL NOT, SHOULD,
SHOULD NOT, RECOMMENDED, MAY, and OPTIONAL in this document are to be
interpreted as described in RFC2119.

CI merges the pull requests. Nobody has to watch them.

## 1. What it is

`.github/workflows/auto-merge.yml` runs a sweep
(`.github/scripts/auto-merge-sweep.sh`) over the open pull requests and
merges every PR that is

1. not a draft,
2. not labeled `automerge:block` or `needs-human`,
3. mergeable with `main` without a conflict, and
4. covered by a **green CI gate that was tested against the current
   `main`** (see §3).

The merge is a **merge commit** (`gh pr merge --merge`), the same shape as
the existing `main` history. The branch is left in place.

This replaces the "a human reviews the PR" rule of the orchestrator
command (`.opencode/commands/issue-orchestrator.md`): no follow-up ticket
is created to track a CI run, and no agent merges a PR by hand.

## 2. When it runs

| Trigger | Why |
|---------|-----|
| `pull_request_target` (opened, synchronize, reopened, ready_for_review) | a new PR or a new push can turn another PR green |
| `workflow_run` on a gate workflow (completed) | the green verdict arrives here |
| `push` on `main` | a merge invalidates the merge refs of the other PRs |
| `workflow_dispatch` | a manual sweep (`dry_run` input: report only, merge nothing) |

The workflow always checks out `main` and always runs the script from
`main`. A pull request therefore cannot change the rules that merge it,
and the workflow file itself is reviewed as a normal change.

## 3. The gate workflows

The gate is configured in the workflow (`AUTOMERGE_GATE_WORKFLOWS`):

```
backend-tests=integration+schemathesis,frontend-checks=checks
```

Each entry is a workflow name and the jobs in it that must be green.

- A gate workflow that did **not** run for a PR (its path filter excluded
  the PR) is not counted. A backend-only PR is judged by `backend-tests`,
  a frontend-only PR by `frontend-checks`.
- **At least one** gate workflow must have run and be green: a PR without
  any check is not a pass.
- Every gate workflow that ran must be green.

`frontend-checks` is not on `main` yet (open PR #125). Until it lands,
only `backend-tests` is a gate, and a frontend-only PR is not merged
automatically (no gate ran for it).

## 4. The merge rule

For every open PR the sweep asks GitHub for the current state and merges
the PR only when all of the following hold:

- `isDraft` is false.
- The labels do not contain `automerge:block` and do not contain
  `needs-human`.
- `mergeable` is true and `mergeable_state` is not `dirty` (conflict),
  not `blocked` and not `unstable`.
- A gate run exists that
  - belongs to the **current head commit** of the PR,
  - tested the PR against the **current `main`** — a `pull_request` run
    whose `base.sha` is the current `main`, or a run dispatched on the PR
    merge ref (`refs/pull/<n>/merge`), and
  - has the blocking jobs completed and successful. The other jobs must
    be successful, skipped or neutral.
- The legacy commit status of the head commit is not failing.

Right before the merge the sweep re-reads the PR: if the head commit or the
merge ref moved while the sweep was running, the PR is skipped.

## 5. Why "green against the current main" and not just "green"

The `pull_request` run of a gate workflow tests the **merge result** (the
merge ref), not the branch alone. When `main` moves, every open PR gets a
new merge ref, and the old green result says nothing about the new one.
The sweep therefore compares the base SHA of the run with the current
`main` SHA and merges only a run that was tested against the current
`main`.

Consequence: **one PR is merged per CI cycle.** After a merge, `main`
moved, so the other PRs need a new CI run against the new `main` before
they can be merged. With a PAT, GitHub queues that run automatically; the
sweep merges the next PR when it comes back green. This is the behaviour
of a merge queue without the queue.

The sweep also refuses to merge anything while the gate on the current
`main` is not green (`AUTOMERGE_REQUIRE_MAIN_GREEN`, default `true`). A
red `main` is never made worse by a new merge.

## 6. The token: `GITHUB_TOKEN` vs a PAT

A push made with the repository `GITHUB_TOKEN` triggers **no** workflow
run. That has two consequences:

- the gate on `main` does not start after the sweep merges a PR, and
- the other PRs do not get their new CI run against the new `main`.

The sweep handles both by dispatching the gate workflows by hand
whenever it merged with the `GITHUB_TOKEN`. It also dispatches the gate for a
PR that has no green run against the current `main` (a green run on the head
against an older `main` does not count), and skips that PR in this sweep; a
later sweep merges it once the verdict is in. A dispatch **cannot** target a
PR merge ref (`--ref refs/pull/<n>/merge` answers `No ref found`), so the
gate workflows take the input `pr_number=<n>`:

- they check out `refs/pull/<n>/merge` themselves (the same thing the
  `pull_request` event tests),
- they report the verdict as the commit status
  `automerge/merge-ref/<workflow>` on the head commit of the PR, with the
  merge ref they tested in the description.

The sweep reads that status and merges the PR only when the status is
green **and** names the merge ref that is current now. A stale verdict is
ignored, exactly like a stale CI run.

> The `frontend-checks` workflow (open PR #125) does not have the
> `pr_number` input yet. With the `GITHUB_TOKEN` a frontend PR therefore
> gets no fresh gate after a merge, and waits. With a PAT (below) it does
> not matter.

**Recommended:** add a repository secret `AUTOMERGE_TOKEN` (a PAT with
`contents: write` and `pull-requests: write`). With a PAT the push
triggers the workflows by themselves, the hand-made dispatch is not
needed, and the whole chain is native. The sweep uses
`secrets.AUTOMERGE_TOKEN` when it exists and falls back to
`secrets.GITHUB_TOKEN`.

## 7. Escape hatches

| Lever | Effect |
|-------|--------|
| label `automerge:block` | the PR is never auto-merged |
| label `needs-human` | the PR is never auto-merged |
| draft | the PR is never auto-merged |
| `workflow_dispatch` with `dry_run: true` | the sweep reports what it would merge and merges nothing |
| `AUTOMERGE_REQUIRE_MAIN_GREEN=false` (repository variable) | the sweep merges even when `main` is red |
| `AUTOMERGE_DISPATCH_CI=never` | the sweep never dispatches the gate by hand |
| `AUTOMERGE_MERGE_METHOD=squash\|rebase` | a different merge method |
| `AUTOMERGE_GATE_WORKFLOWS` | which workflows are the gate, and which of their jobs must be green |
| `AUTOMERGE_BLOCKED_PATHS` | the paths a PR may not touch to be auto-merged (empty by default) |

The brake is the label: `automerge:block` and `needs-human` are added by
the owner, by hand, when a PR must not be merged automatically.

## 8. What happens after a merge

The sweep posts one comment on every PR it merges, so the history of a
merged PR shows that CI merged it and not a human. It also drops the
`agent:in-progress` and `agent:needs-review` labels from the issue the
merged PR closes (`Closes #<n>`): those labels are the orchestrator's
bookkeeping and MUST NOT outlive the merge.

## 9. Operating the sweep

```sh
# the self test of the decision logic
bash .github/scripts/auto-merge-sweep.test.sh

# a manual dry run: what would be merged right now?
gh workflow run auto-merge.yml --ref main -f dry_run=true
gh run list --workflow auto-merge.yml --limit 5

# the log of the last sweep
gh run view <run id> --log
```

The run summary lists every open PR with the decision and the reason.
