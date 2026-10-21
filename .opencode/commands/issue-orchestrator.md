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
   issue, and set the labels to `agent:in-progress` (done) or
   `agent:failed` + `agent:supervised` (needs a human).
5. **Verify.** The PR exists, points at `agent/issue-<N>`, its base is `main`,
   and the tests ran. If the run failed or timed out: `agent:in-progress` →
   `agent:retry-N` (N = 1..3) and re-add `agent-ready`; after the third
   failure, `agent:failed` + `agent:supervised` with a comment saying the
   retry budget is spent.
6. **Link.** Link the issue to this orchestrator session with `session.link`
   as soon as it is known.

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
