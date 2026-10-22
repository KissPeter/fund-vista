# fund-vista issue orchestrator (run 2026-10-22)

Source of truth: `.opencode/commands/issue-orchestrator.md` (repo), hourly cron job
"Issue orchestrator" in OpenChamber (prompt: the command file).

Labels: agent-ready (queue), agent:in-progress (a live worker owns it right now),
agent:needs-review (PR open, waiting for the human), agent:retry-1..3, agent:failed,
agent:supervised (human triage), priority:p0/p1/p2.

Run 2026-10-22 (session ses_e9e32cb4affe5AzvTHU2RXVqaI):
- Queue was empty (no open agent-ready); every open issue was in-progress or supervised.
- Filed #85 (orchestrator command lacked a CI-watch rule + observation capture) and
  #86 (PR #71 and #83 touch the same test hunk in backend/tests/test_penplot_drawing.py
  plus docs/index.md -> they conflict in any landing order; #83 carries the canonical
  OCR fix).
- Implemented #85 in worktree ~/PycharmProjects/fund-vista.worktrees/2 on branch
  agent/issue-85 -> PR #87 (docs-only, merge-clean vs PR #78's branch). CI-watch issue #88.
- Created the label agent:needs-review; relabelled issues 64, 66, 76, 80 from
  agent:in-progress to agent:needs-review (their PRs are open and green).
- Nothing is merged by the orchestrator: a human merges the PRs.

Run 2 (same session, issue #86):
- PR #71 (agent/issue-64) and PR #83 (agent/issue-80) both changed the same hunk of backend/tests/test_penplot_drawing.py and both appended a row at the end of docs/index.md -> conflict in any order. Fixed: the duplicate OCR hunk was removed from agent/issue-64 (commit "revert(penplot): the OCR test fix belongs to PR #83"), the B-002 row moved next to B-001 (agent/issue-80), the CR-007 row moved next to CR-002 (agent/issue-66, that pair was found by the pairwise scan). Result: git merge-tree is conflict-free for every pair of the 13 open agent branches, in both orders. PR #89 (docs/REF-003-pr-landing-order.md) records the rule. Watch issue #90. Landing order: #83 -> #71 -> the rest, any order.
- Rules of thumb: one canonical fix per problem; every branch adds its docs/index.md row at its own anchor; check with git merge-tree <branchA> <branchB> before pushing.

Lessons: never put backticked commands in an `gh pr create --body` shell string
(backtick substitution eats them) - use --body-file. `gh issue create --label` aborts
the whole create when one label is missing, so create labels before using them.