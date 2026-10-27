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
the whole create when one label is missing, so create labels before using them.# fund-vista issue orchestrator (run 2026-10-22)

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
Run 3 (session ses_e9dfbddf9ffebcYdHuALQTauVT, hourly cron 2026-10-22 ~05:1x):
- Queue was empty again (no open agent-ready). Closed the three stale watch issues
  #82 (PR #81 CI green), #88 (PR #87 docs-only, no checks) and #90 (PR #89 docs-only,
  no checks) with green/vacuous comments - the human merges, the orchestrator does not.
- All 10 open PRs verified green (integration + pytest + schemathesis), no red run since
  2026-10-22T04:26Z. PR #87 / PR #89 are docs-only: no CI workflow triggers on them.
- Observation capture filed 11 tickets (#91-#102 minus the watches): #91 npm ci ERESOLVE
  (the Pages deploy has been red on main since 2026-10-21T05:03Z - vite 8 vs
  @vitejs/plugin-react-swc 3.11.0 peer range), #92 cancelling-while-queued burns the per-IP
  job quota, #93 jobs:pending never consumed + no restart recovery, #94 proxy cache decode
  500 + redis_client=None on a Redis error, #95 traffic slice store fixed .tmp name,
  #96 localStorage JSON.parse throws in loadYieldData, #97 NaN portfolio math,
  #98 provider-switch race, #100 node_modules//dist/ not gitignored, #102 no frontend CI on PRs.
- Picked #91 (lowest agent-ready) -> worktree 6, branch agent/issue-91 -> PR #99
  (fix(deps): @vitejs/plugin-react-swc 4.3.3; npm ci + lint(0 errors) + build verified).
  #91 relabelled agent:needs-review; watch issue #101 tracks the post-merge Pages deploy.
- Rules of thumb (new): `npm ci` is the real frontend gate - check the Pages workflow
  history, not just the backend CI. A PR that touches only frontend/docs gets NO checks,
  so "no checks reported" means "nothing was run", not "green".
- The memory file is tracked on branch agent/issue-86 (PR #89) but untracked in the main
  checkout: appending here does not conflict with that branch (keep it out of new branches).

Run 4 (issue #92, worktree ~/PycharmProjects/fund-vista.worktrees/7, branch agent/issue-92):
- Queue: agent-ready issues #92 (p1), #93/#95 (p1), #94/#96/#97/#98/#100/#102 (p2),
  #103 (unlabeled). Lowest number wins -> #92.
- The dispatched worker session (ses_e9dc42b09ffe7D3dn6DS2MBLyS) stayed idle with no
  assistant message after ~2 min -> orchestrator fallback: the orchestrator did the work
  itself in worktree 7. NOTE: session.create with `branch`+`startRef` created the session
  with directory = the MAIN checkout, not the worktree - pass `directory` explicitly and
  verify it.
- Fix: runner.drop_queued became `async` (awaited by the router): it cancels the queued
  task AND writes the terminal `cancelled` state; the cancel_previous supersede path drops
  the queued job too; _run_job refuses to start when the store doc is already terminal
  (set_status does not resurrect a terminal doc).
- Test: backend/tests/test_jobs_cancel_quota.py (live HTTP, uvicorn on a socket). The
  `queued` window is simulated by monkeypatching `jobs_runner.enqueue` to a no-op - the job
  then never leaves the 'queued' stage. RED pre-fix ('cancelling' != 'cancelled'), GREEN
  post-fix.
- runtests.sh: the host path /opt/fund-vista does not exist on this Mac - build the dev
  image (`docker build -t fundvista-dev .` + `pip install pytest httpx` layer) and mount the
  WORKTREE path instead: `docker run --rm -v <worktree>:/srv/fund-vista -e
  PYTHONPATH=/srv/fund-vista ... fundvista-dev python -m pytest backend/tests/ -q`.
  742 passed. The local `uv run` venv lacks tesseract -> test_penplot_drawing.py OCR test
  fails there (pre-existing, fixed by the open PR #83).
- PR #104 -> base main, CI green (integration pytest + schemathesis smoke). Issue #92
  relabelled agent:in-progress -> agent:needs-review. A human merges.
- Follow-up (same run, protocol from PR #87): CI-watch issue #105 "[ci-watch] #92 / PR #104"
  (agent:in-progress + priority:p1) tracks the post-merge main CI + the Mac deploy + the
  Redis-path behaviour check. Observation capture filed #106 (the dispatched worker session
  reported the MAIN checkout as its directory and never started), #107 (the MCP wrapper
  rejects boolean args: session.create returnResult, session.list all), #108 (runtests.sh
  mounts /opt/fund-vista, which does not exist on this Mac). Evidence for the tesseract
  observation was added to the existing #80 instead of a duplicate. Closing comment on #92
  names #105-#108.
- Conflict scan: `git merge-tree --write-tree` of agent/issue-92 against every other open
  agent branch is clean (only the stale, already-merged agent/issue-58 conflicts - add/add on
  the old vendored-font files, irrelevant). PR #104 is merge-clean vs main.

Run 5 (issue #94, worktree ~/PycharmProjects/fund-vista.worktrees/9, branch agent/issue-94):
- Queue: agent-ready #94, #95, #96, #97, #98, #100, #102, #103, #106, #107, #108.
  Lowest number wins -> #94.
- The dispatched worker session (ses_e9ce62d43ffe3CfIFkYdFKbv52) stayed idle with
  NO assistant message after ~2 min -> orchestrator fallback again. session.create
  with `directory` DID report the worktree, but session.messages still reported
  directory = the MAIN checkout: the session never started (reproduces #106).
- Fix (backend/main.py): the cache read no longer nulls the module global
  redis_client on a RedisError (a Redis error is a cache miss); a malformed entry
  (ValueError/binascii.Error/KeyError/TypeError from json.loads/base64) is caught,
  the poisoned key is deleted via `_delete_cache_entry` (best effort, RedisError
  swallowed) and the request falls through to the upstream. base64 decode is strict
  (validate=True).
- Test: backend/tests/test_proxy.py - 3 new live-HTTP cases (corrupt entry -> 200 +
  key deleted; entry without "body" -> 200 + key deleted; transient Redis error ->
  MISS then HIT, redis_client unchanged). RED before the fix (3 failed), GREEN after.
- The FULL suite needs the docker dev image; the local `uv` venv run fails 6 traffic
  tests (host disk low -> the traffic store's free-space floor, environmental) and
  misses tesseract. In the container: 742 passed.
- IMPORTANT: the host disk hit 100% and the Rancher Desktop VM ext4 aborted its
  journal -> every `docker run`/`build`/`prune` failed with
  "read-only file system" on containerd's meta.db. Recovery: `rdctl shutdown` +
  `rdctl start` (containers come back via unless-stopped). Filed #111.
  Also freed ~/Library/Caches/ms-playwright + rancher-desktop-updater (~2.6 GB).
- PR #109 -> base main, CI green (integration pytest + schemathesis smoke).
  Issue #94 relabelled agent:in-progress -> agent:needs-review. CI-watch issue #110.
  Observation #111 (docker read-only after a full host disk).
- Merge-clean vs every open agent branch (git merge-tree) except the unpushed
  local agent/issue-93 (same tree as main -> clean).
  Follow-up in the same run: added the missing binascii.Error coverage
  (a JSON entry whose body is not valid base64 -> falls back to the upstream).
  RED vs origin/main: 4 failures; GREEN after: 743 passed in the container.
  GOTCHA: while verifying RED I ran `git checkout origin/main -- backend/main.py`
  to test the new tests against the unfixed code; that staged the OLD main.py in
  the index, and the next `git commit` (which staged only the test file) still
  committed the reverted main.py -> CI went red (4 failed). Fix: a follow-up
  commit restoring the fix (ec67613). NEVER `git checkout <ref> -- <file>` in a
  worktree before committing; use `git stash` or a copy + `git restore` afterwards.
  PR #109 is green again (integration pytest + schemathesis smoke).

Run 6 (issue #95, worktree ~/PycharmProjects/fund-vista.worktrees/10, branch agent/issue-95):
- Queue: agent-ready #95 (p1), #96/#97/#98/#100/#102 (p2), #103, #106/#107/#108, #111.
  Lowest number wins -> #95.
- The dispatched worker session (ses_e9cb1b28effe8rL9S1Eu2WA3Qa) stayed idle with NO
  assistant message after ~2 min -> orchestrator fallback again. session.create was given
  `directory` = the worktree and the create response echoed the worktree, but
  session.messages still reported directory = the MAIN checkout: reproduces #106 again.
- Fix (backend/traffic/store.py, FileSliceStore.put): the temp file name now carries the
  pid + uuid4 hex, is created exclusively ("xb"), is removed if the write fails, and is
  published with a single atomic os.replace. Module docstring updated.
- Test: backend/tests/test_traffic_store_concurrency.py - two writer threads race on the
  same day key (the published file must decompress as valid gzip and equal one payload,
  no *.tmp residue) + two writes must use different temp names. RED vs the old code
  (both new tests fail), GREEN after.
- Tests: runtests.sh is still broken on the Mac (mounts /opt/fund-vista, #108) -> ran the
  same container command as run 4 with the WORKTREE path mounted: 741 passed.
- PR #112 -> base main, head agent/issue-95, CI green (integration pytest + schemathesis
  smoke). Issue #95 relabelled agent:in-progress -> agent:needs-review. CI-watch issue #113.
- Merge-clean vs every open agent branch (git merge-tree --write-tree): CLEAN for
  agent/issue-{94,92,91,86,85,80,1061,1095,76,66}.
- Rules of thumb (new): the worker session's directory is unreliable - verify it before
  trusting it, and use the orchestrator fallback instead of waiting.

Run 7 (issue #96, worktree ~/PycharmProjects/fund-vista.worktrees/11, branch agent/issue-96):
- Queue: agent-ready #93 was already IN PROGRESS (the 09:10 run owned it), so the lowest
  unclaimed p2 was #96 (the frontend localStorage yield-cache bug). #111 and #106 are also
  p2 but were not the lowest number.
- COLLISION (filed as #118): the 05:10 run and the 09:10 run BOTH picked #93 and both
  edited backend/jobs/{schemas,store}.py in worktree 8 at the same time. The two designs
  diverged mid-flight (my CLAIM_TTL_S constant vs their ZSET+claim design) and I had to
  `git checkout -- backend/jobs/schemas.py` to back my edits out before re-picking.
  LESSON: before editing a worktree, check `git worktree list` + the branch's session
  ownership; if another session is already in that worktree, back out and re-pick.
- Disk: the host volume was at 100% (483 MiB free) which is what silently killed the
  earlier shell calls and the worktree creation. Freed with `docker image prune -f` +
  `docker builder prune -f` (~1.4 GB back). `runtests.sh` still broken on the Mac (#108).
- Frontend has NO test runner (#102 mentions no CI; there is no runner at all). Fix: the
  tests use node's built-in runner (`node --test`, node 26 runs .ts directly) with a
  `npm test` script -> no new dependency. GOTCHA: node's ESM resolver needs the explicit
  ".ts" extension in relative imports, and tsconfig.app.json must keep
  allowImportingTsExtensions (it does) or `tsc --noEmit` rejects it.
- Fix (frontend): readJsonCache (src/services/jsonCache.ts) treats an unparsable or
  wrongly shaped localStorage entry as a MISS and drops it; the yield cache + load loop
  moved to src/services/yieldCache.ts (unit-testable without a DOM); the loading flag is
  cleared in a `finally`; every loadYieldData call site catches the rejection + toasts.
- Tests: src/services/{jsonCache,yieldCache}.test.ts - 11 tests. RED verified by
  re-introducing the unguarded JSON.parse (the yield-cache test file then failed to load).
  npm run lint 0 errors (9 pre-existing warnings), npm run build ok, backend 739 passed.
- npm install died with EACCES on the shared ~/.npm cache (root-owned files) -> filed as
  #119; workaround is `npm install --no-save --legacy-peer-deps --cache <writable dir>`.
- PR #114 -> base main, head agent/issue-96. Issue #96 relabelled agent:in-progress ->
  agent:needs-review. CI-watch issue #115 (note: no CI lane runs for a frontend-only PR,
  so the watch is local-verification only until #102 lands).
- Observations filed this run: #118 (two sessions on one issue/worktree), #119 (root-owned
  npm cache). No duplicates of the existing #100/#106/#107/#108/#111.

Run 7b (issue #96 hardening, worktree 11, branch agent/issue-96, commit 08768b5):
- Audited the delivered PR #114 instead of picking a second issue (one issue per run, and
  the 14:10 session now owns #97/#98 - picking again would repeat the #118 collision).
- Found 2 real gaps in my own fix: (a) the cache helpers still called localStorage.
  removeItem/setItem unguarded, so "a storage call throws inside the loader" (the #96
  class) was still reachable; (b) setProgress(0) sat BEFORE the loader's try, so a throw
  there skipped the finally and left the loading flag stuck - the exact #96 symptom.
- Fix: removeCacheEntry/writeJsonCache (best effort - a cache is an optimisation, a failed
  write must not fail a load whose data was already fetched); setProgress(0) moved inside
  the try.
- Tests 11 -> 16. RED verified by re-introducing the pre-hardening code (5 of the new
  tests fail). Gates: lint 0 errors, build ok, tsc --noEmit clean, backend 739 passed.
- PR #114 pushed to 2 commits, still MERGEABLE/CLEAN, merge-clean vs all 13 open agent
  branches. Watch issue #115 and issue #96 updated.
- LESSON: after writing a fix, audit the OTHER storage/IO calls on the same path, not
  just the one the issue names - the same failure class usually hides in the neighbours.

Run 8 (issue #100, worktree ~/PycharmProjects/fund-vista.worktrees/15, branch agent/issue-100):
- Queue after run 7: #93 (09:10 session), #97/#98 (14:10 session) were owned; #100 was the
  lowest unclaimed p2 -> picked. Collision check first: no branch agent/issue-100, no worktree,
  issue still agent-ready.
- Fix: .gitignore gains node_modules/, dist/ and .venv.
  GOTCHA (worth remembering): `.venv/` (trailing slash) does NOT match a SYMLINK - git treats a
  symlink as a file. The worktree recipe symlinks backend/.venv, so the rule must be `.venv`
  without the slash. Verified with git check-ignore in a scratch repo.
- Verified the DoD for real (not just the ignore rules): after npm install + npm run build,
  `git status --porcelain -uall` lists ONLY the intended change while 254 MB node_modules/ and
  the built dist/ are on disk; `git add -A` stages 1 file instead of the tree; 1316 tracked
  files before and after, no deletions, .env.example still visible.
- npm ci FAILS on main with the ERESOLVE of #91 (fix is the open PR #99, cannot merge).
  Used `npm install --no-save --legacy-peer-deps --cache <writable dir>` instead; noted it in
  the PR body and in a comment on #102 (the frontend-CI ticket) so a future workflow does not
  go red on it.
- Added evidence to #102 instead of filing a duplicate: the frontend NOW HAS a test lane
  (npm test -> node --test, no new dependency, 16 tests) and tsc -p tsconfig.app.json --noEmit
  is the type check (npm run build does not type-check).
- PR #123 -> base main, head agent/issue-100. Issue #100 -> agent:needs-review. Watch #124.

Run 9 (issue #102, worktree ~/PycharmProjects/fund-vista.worktrees/13, branch agent/issue-102):
- Queue: agent-ready #102 was the lowest (others: #103, #106, #107, #108, #111, #118, #119,
  #120, #122). No open PR for #102. Picked.
- The dispatched worker session (ses_e9c3ea8efffeLUxq2Gu9o6gxbT) FAILED with "Upstream
  request failed: Insufficient account funds" and never produced an assistant message ->
  orchestrator fallback: did the work itself in worktree 13.
- Fix: new .github/workflows/frontend-checks.yml (install -> lint -> typecheck -> build ->
  test) on frontend paths, pull_request + push to main, pinned Node 24 / setup-node@v4 /
  cache npm (same pattern as deploy-pages.yml), ubuntu-24.04 (same as backend-tests.yml).
  package.json gains "typecheck": "tsc -p tsconfig.app.json --noEmit".
- Two deliberate workarounds, both commented in the workflow:
  (a) `npm ci --legacy-peer-deps` - plain npm ci still ERESOLVEs on main (#91, fix is the
      UNMERGED PR #99), so a plain `npm ci` lane goes red before it starts. The flag changes
      no resolved version and is dropped together with PR #99.
  (b) `npm test --if-present` - the frontend test lane is the UNMERGED PR #114, so main has
      no test script yet; the step starts running the tests for real once #114 lands.
- GOTCHA (cost 3 amend-commits): the package.json scripts block is a CONFLICT HOTSPOT.
  PR #114/#116 both append a "test" script after "preview" - appending "typecheck" anywhere
  in the same block conflicts with them. Placing "typecheck" after "build:dev" (an
  untouched anchor) is merge-clean against ALL open agent branches. Verified with
  `git merge-tree --write-tree origin/<branch> HEAD` for every branch. The same anchor
  problem is filed as #120.
- Gates on the branch: npm ci --legacy-peer-deps ok, lint 0 errors / 9 pre-existing
  warnings (no --max-warnings!), typecheck clean, build ok, npm test --if-present ok.
- PR #125 -> base main, head agent/issue-102. The NEW workflow itself ran on the PR:
  frontend-checks SUCCESS (run 39786127464) - the first frontend CI check in this repo.
  Issue #102 -> agent:needs-review. Watch issue #126 (the push-to-main run after merge).
- agent/issue-58 conflicts with main on backend files (add/add on THIRD_PARTY_NOTICES.md
  and the font READMEs) - pre-existing, unrelated to this change.

Run 10 (slot 1, issue #103, worktree ~/PycharmProjects/fund-vista.worktrees/16, branch
agent/issue-103):
- Queue: the lowest agent-ready issue was #103 (no open PR for it). The worktree creation
  FAILED first: the host disk was at 100% (134 MiB free) -> "No space left on device" while
  materialising backend/.data/images/*.svg. Freed ~6 GiB: removed 7 stale worktrees of the
  SHOP repo whose PRs are closed (1015, 994, 1149, 1162, 1194, 943, basecheck; the dirty ones
  958, 1161, 1165 were left alone) + ~/Library/Caches + ~/.cache. The 16 GiB
  rancher-desktop/lima disk image cannot be trimmed while the docker daemon is down, and it
  holds the prod container - do NOT prune it. Evidence commented on #111 and #122.
- The dispatched worker (ses_e9c084bc8ffehllZXr52HIhEYs) FAILED with "Upstream request
  failed: Insufficient account funds" and never produced an assistant message -> orchestrator
  fallback: did the work itself in worktree 16. Filed #128 (the fallback only covered an
  IDLE worker, not an errored one).
- Fix (docs-only): backend/THIRD_PARTY_NOTICES.md gets a new section D ("a backend/uv.lock
  change needs a shop-side regeneration" - the shop's tools/licenses/inventory.py reads
  fund-vista/backend/uv.lock and stamps its digest into docs/third-party-inventory.md; the
  shop's vitest masks only the package-lock.json digest, so the uv.lock digest is gated ->
  the shop must run `python3 tools/licenses/inventory.py` and commit), plus
  .github/PULL_REQUEST_TEMPLATE.md (new) with the uv.lock checklist item, docs/CR-008 + the
  index row, and a Serena memory (ops/shop-licence-inventory-coupling.md).
- GOTCHA (docs/index.md): a new row + a new "Doc:" line at the END of the table conflicted
  with agent/issue-64's row (same anchor). Fix without a force push: brute-force the anchors
  with `git merge-tree --write-tree` over one candidate commit per anchor and take the
  anchor that is clean against every open branch (here: after the CR-004 rows) -> a second
  commit. The same trick applies to the package.json scripts block (#120).
- Test: `python -m pytest backend/tests -q` with the MAIN checkout's backend/.venv
  interpreter (a worktree has no venv of its own; `uv sync` would need disk we did not have)
  -> 738 passed, 1 skipped.
- PR #127 -> CI green (integration + schemathesis + the shop-side lanes). Issue #103 ->
  agent:needs-review. Watch #129 (closed green). Observations: #128 filed.

Run 11 (slot 2, issue #106, worktree ~/PycharmProjects/fund-vista.worktrees/17, branch
agent/issue-106):
- Queue: the lowest agent-ready issue was #106. Dispatched with an EXPLICIT free model
  (opencode-go/minimax-m3.1-flash-free): the default model (opencode/muse-spark-1.3) runs
  out of funds ("Insufficient account funds") and the worker NEVER starts. ALWAYS pass a free
  model to session.create.
- The worker DID start and DID work in worktree 17 (its edit landed there, the main checkout
  stayed clean) -> the "session.messages reports the MAIN checkout" notes in runs 5/6 are a
  MISREADING: session.status.directory and session.messages.directory report the session's
  PROJECT directory (a scope echo), NOT its working directory. Proof: a probe session created
  with directory=worktree 16 answered pwd -> .../worktrees/16, git branch --show-current ->
  agent/issue-103, git rev-parse --show-toplevel -> .../worktrees/16, while session.status said
  directory=/Users/kissp/PycharmProjects/fund-vista. The create response's `directory` field
  only echoes the requested path.
- The worker's first PR version made `session.status.directory` a HARD assertion -> it would
  fail on EVERY dispatch and send every run to the fallback. The orchestrator sent it back
  with the evidence; the follow-up commit f0beb7d replaced it with the checks that are true:
  the worker's own self-check (pwd/toplevel/branch, stop if wrong) + the orchestrator's
  outside check (the main checkout must still be clean after the dispatch).
- PR #130 (docs-only; NO CI is expected - backend-tests.yml is path-filtered to backend/**,
  runtests.sh, ops/schemathesis-smoke.sh, so a .opencode-only PR gets no checks at all).
  Merge-clean vs every open agent branch, incl. agent/issue-85 (same command file).
  Issue #106 -> agent:needs-review. Watch #131 (closed). #128's agent-ready removed (its gap
  is closed by PR #130).

Rules of thumb (new, run 11):
- A dispatched worker needs an explicit free model, otherwise it dies on arrival with
  "Insufficient account funds" (the fallback saves the run, but the worker is wasted).
- `session.status.directory` / `session.messages.directory` are NOT the session's working
  directory - they echo the queried scope (the project root). Assert the worker's place with
  its own pwd/toplevel/branch, and assert the worktree rule from outside (the main checkout
  must stay clean).

Run 12 (slot 3, issue #107, worktree ~/PycharmProjects/fund-vista.worktrees/18, branch
agent/issue-107):
- Queue: the lowest agent-ready issue was #107. Fresh evidence first: the MCP wrapper now
  ACCEPTS real booleans - `session.list` with `all: true` answers "archived":"included", and
  `session.create` with `returnResult: true` was accepted in the runs for #103 and #106. The
  coercion bug of #107 is gone from the wrapper (it lives outside this repo).
- Delivered the in-repo half: one bullet at the end of the "## Rules" list of
  .opencode/commands/issue-orchestrator.md records the working call shapes and the
  boolean-free fallback (omit returnResult, poll session.messages).
- ANCHOR GOTCHA (the file's Rules list is contended): the requested anchor (after the last
  rule line) CONFLICTS with agent/issue-85, which appends two bullets at exactly that spot.
  The worker moved the bullet one line up (before "- Do not merge: ...") and re-checked.
  Verified independently: git merge-tree --write-tree -> 0 conflicts against all 19 open
  agent branches (incl. issue-85 and issue-106, same file).
- PR #133 (docs-only; no CI expected - backend-tests.yml is path-filtered to backend/**).
  Local suite: 738 passed, 1 skipped. Issue #107 -> agent:needs-review. Watch #134 (closed).
- Observation filed: #132 (the default dispatch model has no funds - always pass a free
  model to session.create, e.g. opencode-go/minimax-m3.1-flash-free).
