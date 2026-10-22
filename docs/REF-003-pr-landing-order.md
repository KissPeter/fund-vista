# REF-003 — PR landing order and the `docs/index.md` anchor rule

Untracked note (no F/CR/B id), written by the opencode-orchestrator run of
2026-10-22 for issue #86.

## Why this note exists

`docs/index.md` is an append-log: every finished item adds one table row
(and one `Doc:` line). While several agent PRs were open at the same time,
each of them appended its row at the **same anchor** (the end of the table),
so the second merge of any two of them conflicted in that table — and the
`docs/index.md` conflict was mixed with a real code conflict in
`backend/tests/test_penplot_drawing.py`, which PR #71 and PR #83 both
changed from the same base blob.

Two rules keep the next round conflict-free.

## Rule 1 — one canonical fix per problem

The OCR-backend failure of issue #80 was fixed twice: PR #83 (the dedicated
fix: the production `KeyError: None` → warning degrade, host-independent
OCR tests, the README note, `docs/B-002-*.md`) and PR #71 (a stopgap hunk
in the same test). The duplicate hunk was removed from `agent/issue-64`
(commit `revert(penplot): the OCR test fix belongs to PR #83`), so the two
branches touch disjoint content.

**Landing order: PR #83 first, then PR #71.** If #71 lands first, `main`
carries the false-red OCR test for a short time (CI is green either way —
CI installs `tesseract-ocr`); #83 then lands the canonical fix.

## Rule 2 — every branch adds its `docs/index.md` row at its own anchor

Two branches that insert at the same anchor conflict even when the rows are
unrelated. The current placement of the open rows:

| Row | Branch | Anchor |
|---|---|---|
| `B-002` | `agent/issue-80` (PR #83) | after the `B-001` row (bug rows together) |
| `CR-006` | `agent/issue-64` (PR #71) | after the `CR-005` row (end of the table) |
| `CR-007` | `agent/issue-66` (PR #73) | after the `CR-002` row (early CR group) |

The table already splits the CR rows into two groups (`CR-001`/`CR-002` at
the top, `CR-003`+ at the bottom), so `CR-007` joining the top group keeps
the table readable and gives every branch its own anchor.

**When you add a row:** pick an anchor that no other open branch uses, and
check it with `git merge-tree` before pushing (below). If the anchor you
want is taken, use a neighbouring group (bugs with the bugs, CRs with the
CRs) instead of appending at the end.

## Verifying a merge before the human merges it

```sh
git fetch origin --prune
git merge-tree origin/agent/issue-64 origin/agent/issue-80   # empty output = clean
```

`git merge-tree <a> <b>` prints the conflicted files; no output means the
merge is clean. To scan every open agent branch pairwise:

```sh
br=(${(f)"$(git branch -r | grep -E 'origin/agent/' | sed 's/ *origin\///')"})
for a in $br; do for b in $br; do
  [[ "$a" < "$b" ]] || continue
  n=$(git merge-tree "$a" "$b" 2>&1 | grep -c CONFLICT)
  [ "$n" = "0" ] || echo "CONFLICT: $a x $b"
done; done
```

State at the time of writing (2026-10-22): **no conflicts** between any two
open agent branches — the scan above prints nothing. The dependabot branch
(`dependabot/uv/backend/certifi-2026.10.19`, PR #70) is third-party and
outside the automerge scope; the pen-pixel-shop PRs are a different repo.

## Open PR map (2026-10-22)

| PR | Branch | Issue | CI-watch issue |
|---|---|---|---|
| #71 | `agent/issue-64` | #64 (pydantic validation + ops tools) | — |
| #72 | `agent/issue-1080` | pen-pixel-shop #1080 (traffic layers) | — |
| #73 | `agent/issue-66` | #66 (parallel download, progress counters) | — |
| #75 | `agent/issue-1089` | pen-pixel-shop #1089 (page frame) | — |
| #78 | `agent/issue-76` | #76 (priority labels + queue ordering) | — |
| #79 | `agent/issue-1095` | pen-pixel-shop #1095 (cross-hatch opt-out) | — |
| #81 | `agent/issue-1061` | pen-pixel-shop #1061 (orphan job reaper) | #82 |
| #83 | `agent/issue-80` | #80 (OCR backend missing → warning) | — |
| #87 | `agent/issue-85` | #85 (orchestrator command: watch + capture) | #88 |

PR #78 and PR #87 both touch `.opencode/commands/issue-orchestrator.md`;
they are verified conflict-free (`git merge-tree origin/agent/issue-76
origin/agent/issue-85` → clean).

## The general lesson

A branch that fixes a problem someone else is fixing at the same time is a
conflict waiting to happen, even when the two fixes are each correct. The
orchestrator should decide **which** branch owns the fix, put the other
branch's duplicate on record as an observation, and merge in that order —
not leave both PRs open and let the human discover the collision.
