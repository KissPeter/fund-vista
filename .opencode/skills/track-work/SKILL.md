---
name: track-work
description: Maintain F/CR/B work tracking under docs/ (index.md + item files) with status, dates, and DoD. Use when creating or updating any feature, change request, or bug doc, or when implementation progress changes.
---

# Track Work

## Naming

- `docs/F-<n>-<slug>.md` — feature, `docs/CR-<n>-<slug>.md` — change
  request, `docs/B-<n>-<slug>.md` — bug. Kebab-case slug.
- `<n>` is the next free integer **per prefix** (scan `docs/`; never reuse
  a number, even after deletion).

## Item file front-matter (first lines)

```
- Status: todo | in progress | done
- Created: YYYY-MM-DD
- Last updated: YYYY-MM-DD
```

Plus a Definition of Done checklist adapted from affilio
`docs/impl/DEFINITION_OF_DONE.md`: code rules (settings-only env,
Pydantic v2, route registration), TDD red → green → refactor, test-type
selection (live-server integration default; unit only for algorithms;
markup-only for CDN pages; negative coverage), full suite green, docs +
Serena memory synced, commit per phase, push only when green.

## index.md (source of truth for status)

`docs/index.md` holds one table row per tracked item: ID, type, title,
status, last updated, doc link. Rules:

1. EVERY creation, status change, or edit of a tracked item MUST update
   its row (status + last updated date) in the same commit.
2. Status vocabulary: `todo`, `in progress`, `done` only.
3. Research/finding notes stay untracked (no ID) under a separate
   "Research notes" section.
4. Never mark `done` unless every DoD box in the item file is checked.

## Workflow

1. New work: allocate ID, write item file with phases + DoD, add index row
   (`todo`, today).
2. Starting work: status → `in progress`, update date.
3. Per phase: check DoD boxes as completed, update date, commit
   (`git commit -m "feat(<ID>): phase <M> — <desc>"`).
4. Finished: all boxes checked → status `done`, update date, sync Serena
   memory, commit + push only when the full suite is green.
5. Commit tracking files with `git add docs/*.md
   .opencode/skills/track-work/SKILL.md`; never push unless asked.
6. Never include secrets in docs.

## Worktree convention (MANDATORY, adapted from affilio DoD)

> Tracked implementation work (any `F-`/`CR-`/`B-` item) MUST happen in a
> dedicated git worktree, never in the main working tree. Fixed root:
> `~/PycharmProjects/fund-vista.worktrees/` — allowlist this path once;
> no per-feature permission is needed afterwards.

- [ ] Directory name is the next free **integer** (`1`, `2`, …), never the
  feature name. Scan `~/PycharmProjects/fund-vista.worktrees/` and pick
  the lowest unused number; never reuse a number.
- [ ] Create from a clean, pushed `main`: `git worktree add -b
  work/<ID>-<slug> ~/PycharmProjects/fund-vista.worktrees/<n> main`
  (e.g. `work/CR-002-map-print`).
- [ ] ALL implementation + phase commits happen on the worktree branch;
  the main checkout MUST NOT receive feature commits (tracking-doc
  commits for status flips are the only exception).
- [ ] Push only when the item's full DoD is green, then merge/push the
  worktree branch to `main` on origin.
- [ ] After a successful push, the agent MUST clean up automatically:
  `git worktree remove ~/PycharmProjects/fund-vista.worktrees/<n>`
  (force-remove only if the branch is fully merged). Never leave stale
  worktrees behind.
- [ ] Docs-only changes (no code) may go direct to the main checkout.
