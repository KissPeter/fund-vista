# CR-008 — a `backend/uv.lock` change needs a shop-side licence-inventory regeneration

- Status: done
- Created: 2026-10-22
- Last updated: 2026-10-22
- Type: change request
- Decided: the backend documents the coupling on the backend side, in the
  two places where a dependency bump is actually made — the licence docs
  (`backend/THIRD_PARTY_NOTICES.md`) and the pull-request template.

## Motivation

The shop repo (`KissPeter/pen-pixel-shop`) carries this backend as a git
submodule and generates its own licence inventory from this repo's
lockfile:

- the generator is `tools/licenses/inventory.py` in the shop repo;
- it reads `fund-vista/backend/uv.lock` — this repo's `backend/uv.lock`;
- it stamps a digest of that file into the "Inputs" line of the shop's
  committed `docs/third-party-inventory.md`;
- the shop's unit lane (`src/lib/third-party-inventory.test.ts`) compares
  the committed file with a fresh generator run; only the
  `package-lock.json` digest is masked (npm rewrites that lockfile), so the
  `uv.lock` digest is gated.

So every dependency bump in `backend/uv.lock` reddens the **shop's** unit
lane until the shop regenerates and commits the inventory. The failure
arrives from another repository, the shop's CI log names a docs file (not
this lockfile), and the fix lives in the other repo — so the person who
ran the bump finds out from a red shop CI.

The shop side already made its failure message say what to do
(pen-pixel-shop #1158 landed the drift hint). The backend half was still
missing: nothing in this repo said that a `uv.lock` change needs a
shop-side regeneration (fund-vista #103).

## Scope

- `backend/THIRD_PARTY_NOTICES.md` — a new section D (and two pointers to
  it) stating the coupling, the trigger, the shop-side command and the
  PR-template reference.
- `.github/PULL_REQUEST_TEMPLATE.md` — new; the checklist carries the
  `backend/uv.lock` item, so the person who opens the dependency-bump PR
  sees it while the PR is being written.
- `docs/index.md` — this item's row.
- `.serena/memories/ops/shop-licence-inventory-coupling.md` — the memory
  counterpart (cross-repo coupling is easy to forget).

## Explicitly unchanged

- No backend code, no schema, no dependency, no test — the change is
  documentation only (T0).
- The shop repo is **not** touched by this change: the regeneration is a
  separate, shop-side commit (pen-pixel-shop #1158 / #1165 track it).
- `backend/uv.lock` itself is unchanged — no dependency is bumped here.

## DoD

- [x] The coupling is documented in this repo, in the two places where a
      dependency bump happens (licence docs + PR template).
- [x] Every factual claim is verified against the real files: the
      generator path, the `UV_LOCK` path it reads, the digest masking in
      the shop's test, the regeneration command.
- [x] The tracked item exists and `docs/index.md` carries its row.
- [x] The Serena memory counterpart exists.
- [x] The backend suite is green (or the pre-existing environmental
      failures are listed by name in the PR body).
- [x] The branch is pushed and the PR is open against `main`; the human
      merges.

## Follow-ups

- The shop-side regeneration after the **next** backend dependency bump is
  a separate commit in the shop repo — not part of this change.
- The shop's inventory has no automated "the pointer moved" trigger; the
  coupling stays a documented convention on both sides.
