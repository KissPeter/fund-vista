---
name: test-design
description: Design backend tests with equivalence partitioning + boundary analysis over real HTTP (live server, never TestClient). Use when adding or changing any backend endpoint, job, or pure algorithm.
---

# Test Design

Adapted from affilio `.claude/rules/testing-integration.md` (real-HTTP
MUST, `TestClient` MUST NOT — including the revoked-exception precedent:
no app-local exceptions) and the repo default (live-server integration
first; unit only for algorithms; negative coverage always).

## Test-type selection

- API behavior (endpoints, jobs, status codes, error envelopes) MUST be
  covered by live-server integration tests over **real HTTP**.
- MUST NOT use `fastapi.testclient.TestClient` — not even "just for
  validation-only paths".
- Pure algorithms (classify / filters / geometry / cache keys) MUST be
  plain unit tests: no app import, no network, no server.
- Every endpoint MUST have negative coverage (4xx paths, validation,
  upstream-outage mapping) — not just the happy path.

## Equivalence partitioning

- Partition each input domain into classes that MUST behave identically,
  then write exactly one representative per class PLUS the invalid class:
  - valid classes (e.g. ICAO: `LHBP` cached / uncached; date: in-range /
    out-of-range / malformed);
  - invalid classes (empty, too long, wrong country, unparseable).
- One class MUST map to one test — never assert two classes' behavior in
  a single test (a failure must name its class).
- Upstream states are classes too (cache hit / miss, release present /
  missing, lookup found / not-found / upstream-down) — stub each at its
  boundary function (table below), never with a mock HTTP server.

## Boundary analysis

- For every numeric/range limit, test min−1 / min / min+1 and
  max−1 / max / max+1 where the domain allows it:
  - ICAO length: 0 / 1 / 8 / 9 chars; date window: oldest−1 / oldest /
    yesterday / today (today is always out); 24 h filter window edges;
    free-space floor is NOT boundary-tested (environment-dependent —
    stub `check_free_space`, note it as manual).
- Date boundaries are relative to UTC today — compute them in the test
  (`selectable_range()`), never hardcode, or the suite rots.
- Off-by-one on time comparisons (`t_from <= t < t_to`) MUST have an
  explicit test (event exactly at `t_to` is excluded).

## Real-HTTP mechanics (repo standard)

- In-process uvicorn thread, **module scope** (one server per file —
  per-test servers are too slow), real socket HTTP via `httpx.Client`:
  `uvicorn.Config(app, install_signal_handlers=False)`, daemon thread,
  poll `/healthz` for readiness, `server.should_exit = True` + join on
  teardown.
- Same process ⇒ `monkeypatch` stubs apply to the serving app: stub
  upstream boundaries only —
  - airport lookup → `…router.resolve_airport` / `…_exec_*.resolve_airport`
    (async fake: HU row / foreign row / raise `AirportNotFoundError`);
  - release index → `…availability.asource_status` (async fake) or
    `httpx.MockTransport` for pure-unit level;
  - archive → `…_exec_traffic._download_and_extract` (sync fake);
  - durable store → patch `backend.traffic.store.default_store` to
    `FileSliceStore(tmp_path)`;
  - Redis → dead-port URL (memory fallback); rate limit → `100000`.
- Shared in-process state MUST be reset per test (autouse fixture):
  `jobs_store.reset_memory()`, `jobs_runner.reset_local()`,
  `configure_*_redis(None)`, limiter `_memory.clear()`.
- Job tests MUST poll `GET /v1/jobs/{id}` with a deadline (condition
  wait), never a fixed `sleep`; MUST NOT lengthen timeouts to mask flakes.
- No dedicated mock HTTP servers, no new test deps: monkeypatch +
  `MockTransport` cover every boundary with zero ports and zero flakes.

## TDD for fixes (affilio rule)

- Regression test BEFORE the fix; MUST fail pre-fix (RED, verified by
  running it), MUST pass post-fix (GREEN); then re-run the full relevant
  suite.

## Suite hygiene

- Hermetic: no network, deterministic, order-independent, unique tmp
  state per test; a green run is `N passed, 0 failed, 0 errors, 0 skipped`.
- What cannot run hermetically (full-day ingest timing on the host, nginx
  fallback exclusion, live upstream resolves) MUST be listed as manual /
  `@live` follow-ups in the test module docstring — never silently
  skipped.
