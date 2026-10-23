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
4. **Dispatch.** Start an OpenChamber session with `session.create`,
   `directory` set to the prepared worktree and **no** `branch`/`startRef`:
   step 2 already prepared the branch there, and passing them lets the
   session be re-homed to the main checkout. Pass the issue body, the
   Definition of Done, and the repo conventions. The worker must:
   implement the issue, keep `runtests.sh` green, push the branch, open a PR,
   link the issue to its session (`session.link`), comment the PR URL on the
   issue, and set the labels to `agent:needs-review` (the PR is open, waiting
   for the human) or `agent:failed` + `agent:supervised` (needs a human).

   The dispatch prompt tells the worker to check its own place **first**:
   `pwd`, `git rev-parse --show-toplevel`, `git branch --show-current`. If
   they are not the prepared worktree and `agent/issue-<N>`, it stops and
   says so — the orchestrator takes over.

   **Trap.** `session.status.directory` and `session.messages.directory`
   report the session's *project* directory (they echo the scope you query
   them in), not its working directory — never use them as the worktree
   assertion.

   **Check from outside.** After the dispatch, the main checkout must still
   be clean (`git status` in it): a worker that wrote there is a failed
   dispatch — stop it, discard its changes in the main checkout (the
   worktree rule forbids feature commits there) and take over in the
   worktree.

   **Fallback.** Take over whenever the worker produced no work: the
   dispatch failed (the worker stopped on its self-check, or it touched the
   main checkout), the session stays idle without a single assistant
   message (~2 min), **or** `session.status` reports state `error` (upstream
   failure, e.g. "Insufficient account funds"). Each of those is dead on
   arrival — the orchestrator does the work itself in the same worktree,
   then follows step 5 for its own PR.

5. **Verify.** The PR exists, points at `agent/issue-<N>`, its base is `main`,
   and the tests ran. If the run failed or timed out: `agent:in-progress` →
   `agent:retry-N` (N = 1..3) and re-add `agent-ready`; after the third
   failure, `agent:failed` + `agent:supervised` with a comment saying the
   retry budget is spent.
6. **Link.** Link the issue to this orchestrator session with `session.link`
   as soon as it is known.
7. **Capture.** Every run ends with observation capture, also after a failed
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
- The MCP wrapper takes real booleans: `session.create` with
  `returnResult: true` and `session.list` with `all: true` both work
  (verified 2026-10-22, fund-vista #107). If a wrapper build answers
  `... must be a boolean` (the coercion bug of #107), drop the flag
  instead of working around it: omit `returnResult` and poll
  `session.messages` for the worker's progress.
- Do not merge: a human reviews the PR.
- An empty queue is a result, not a silent no-op: when no `agent-ready`
  issue qualifies, the run says so in its session summary and stops. The
  queue is refilled by step 7 (observation capture), never by the
  orchestrator inventing work.
- The worktree of a run stays until its PR lands; it is removed afterwards
  (track-work skill).
- **CI merges the PR.** The auto-merge sweep
  (`.github/workflows/auto-merge.yml`, `docs/auto-merge.md`) merges every
  open PR whose gate is green and whose branch has no conflict with
  `main`. The worker MUST NOT merge the PR itself and MUST NOT open a
  ticket to track the CI status of a PR. A PR that must not be merged
  automatically carries the `automerge:block` label, which the owner
  adds by hand.
