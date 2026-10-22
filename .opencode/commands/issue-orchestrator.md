# Issue orchestrator (fund-vista)

Scheduled job that picks the next GitHub issue for an autonomous agent run in
this repository and drives it to a PR.

Repo: `KissPeter/fund-vista`. Issues live in the same repo (shop-side
pen-pixel-shop issues are out of scope here).

## Label protocol

| Label | Meaning |
|---|---|
| `agent-ready` | queued for the next pick |
| `agent:in-progress` | dispatched, a worker session owns it |
| `agent:retry-1..3` | a run failed; retry budget consumed so far |
| `agent:failed` | retry budget spent, no automatic pickup |
| `agent:supervised` | needs a human (split/decision), the orchestrator will not touch it |

## Workflow

1. **Pick.** List open issues with label `agent-ready`. Exclude any issue that
   also carries `agent:failed` or `agent:supervised` (human triage), or that
   already has an open PR. Take the lowest issue number.
2. **Prepare.** `git fetch origin --prune`; branch `agent/issue-<N>` from
   `origin/main`; worktree under `~/PycharmProjects/fund-vista.worktrees/<n>`
   (next free integer, per the track-work skill).
3. **Announce.** Comment on the issue:
   `🤖 opencode-orchestrator picked this up on <short-sha> (slot 1).`
   Labels: remove `agent-ready`, add `agent:in-progress`.
4. **Dispatch.** Start an OpenChamber session in the worktree
   (`branch: agent/issue-<N>`, `startRef: origin/main`) with the issue body,
   the Definition of Done, and the repo conventions. The worker must:
   implement the issue, keep `runtests.sh` green, push the branch, open a PR,
   link the issue to its session (`session.link`), comment the PR URL on the
   issue, and set the labels to `agent:needs-review` (the PR is open, waiting
   for the human) or `agent:failed` + `agent:supervised` (needs a human).

   **Fallback:** a dispatched worker that stays idle without a single
   assistant message (~2 min) never started — do not wait for it. The
   orchestrator does the work itself in the same worktree, then follows
   step 5 for its own PR.

5. **Verify.** The PR exists, points at `agent/issue-<N>`, its base is `main`,
   and the tests ran. If the run failed or timed out: `agent:in-progress` →
   `agent:retry-N` (N = 1..3) and re-add `agent-ready`; after the third
   failure, `agent:failed` + `agent:supervised` with a comment saying the
   retry budget is spent.
6. **Link.** Link the issue to this orchestrator session with `session.link`
   as soon as it is known.
7. **Watch.** Every PR needs one CI-watch issue: the PR's CI result, not its
   implementation. File it right after the PR is open:
   ```sh
   gh issue create --title "[ci-watch] #<N> / PR #<M>" \
     --body "PR: #<M> (Closes #<N>). Waiting for green CI. Red runs get fixed on this branch, pushed, re-read. Green -> the human merges, then this watch issue closes." \
     --label "agent:in-progress" --label "priority:p1"
   ```
   Both labels exist in this repo (`priority:p1` since #76). Never file a
   watch issue for a PR that already has one, and never watch the same PR
   from two issues.
   Keep watching it: on red, root-cause the failure, fix it on the same
   branch (`agent/issue-<N>`), push, and re-read the CI result; on green,
   leave the merge to the human (never merge) and close the watch issue with
   a comment that names the observations filed in step 8. A watch issue whose
   PR is already merged or closed is closed as stale, with a written reason.
8. **Capture.** Every run ends with observation capture, also after a failed
   run. Harvest what was seen: environment and tooling quirks, pre-existing
   breakage outside the blast radius, doc/test gaps, and follow-ups that were
   deliberately out of scope. An observation that is written nowhere is
   lost — session context does not survive into the next run.
   Dedup before filing, with at least two distinct keyword searches:
   ```sh
   gh issue list --state all --limit 30 --search "<keywords>"
   ```
   - already on an **open** issue → add the new evidence as a comment there,
     do not create a second ticket;
   - already on a **closed** issue but still true → file a new ticket and
     reference the closed one (`Related: #<N>`);
   - new → file **one** ticket per observation (never batch unrelated
     observations):
     ```sh
     gh issue create --title "[obs] <one-line topic>" \
       --body "Observed during #<N>: <what, with file:line / CI run / URL evidence>.
     Why it matters: <risk or opportunity>.
     Suggested next step: <smallest useful action>." \
       --label "agent-ready"
     ```
   The body must stand alone (an agent that never saw the run must be able
   to act on it) and must not contain secrets or raw session dumps. The
   closing comment of the run (issue or watch issue) states the filed
   ticket numbers, or that there were none.

## Review state

`agent:in-progress` means a worker session owns the issue **right now**.
When the PR is open and waits for the human reviewer, the issue carries
`agent:needs-review` instead (create the label once with
`gh label create "agent:needs-review" --color ededed`).

Without that second state, "a worker is running" and "the PR is open" look
identical, and the orchestrator cannot tell a live worker from a finished
one — a finished issue then blocks its own number in the queue forever.
`agent:needs-review` is removed when the PR lands.

## Rules

- One issue per run, one slot. Never two workers on the same issue.
- Never `force push`; linear history; PR from `agent/issue-<N>` to `main`.
- The main checkout stays clean of feature commits (worktree rule from the
  track-work skill).
- Pydantic v2 for all request/response validation; settings only through the
  typed settings object; no inline `os.getenv`.
- Tests: live-HTTP integration by default (see the test-design skill),
  unit only for complex algorithms.
- Do not merge: a human reviews the PR.
- An empty queue is a result, not a silent no-op: when no `agent-ready`
  issue qualifies, the run says so in its session summary and stops. The
  queue is refilled by step 8 (observation capture), never by the
  orchestrator inventing work.
- The worktree of a run stays until its PR lands; it is removed afterwards
  (track-work skill).
