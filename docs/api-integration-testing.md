# API integration testing — standards (ported from affilio)

Ported from affilio's `.claude/rules/testing-integration.md`,
`.claude/skills/testing-runbook/SKILL.md` and its Schemathesis workflow
(`run_schemathesis_local.sh`, CircleCI) — adapted to this repo's stack.
The working summary lives in the `test-design` skill
(`.opencode/skills/test-design/SKILL.md`); this document is the
reference the skill summarizes.

## Test execution policy

- **MUST** run all tests locally against local services (the in-process /
  subprocess uvicorn live servers) — **MUST NOT** run tests against
  remote/staging/production environments.
- **MUST** run the full backend integration suite
  (`python -m pytest backend/tests/ -q`) for backend/runtime-affecting
  changes.
- **MUST NOT** leave skipped tests: a green run is
  `N passed, 0 failed, 0 errors, 0 skipped` (platform-gated skips such as
  the Linux-only `tesserocr` wheel are documented exceptions).
- **MAY** use sanity mode for pure docs/comments/non-executable changes.
- **MUST** run the full relevant suite if uncertainty exists.

## Integration constraints

- **MUST** use real HTTP requests (`httpx`) against a running live server
  on a real socket — full stack: ASGI server, routing vs. catch-all proxy,
  error handlers.
- **MUST NOT** use `fastapi.testclient.TestClient` for integration
  scenarios (sole documented exception: `backend/tests/test_jobs_contract.py`,
  which predates the rule and cross-checks the jobs runner; prefer porting
  its cases to live-server equivalents when touched).
- For time-based logic, **MUST** use deterministic state manipulation,
  not `sleep()`.
- Job tests **MUST** poll `GET /v1/jobs/{id}` with a deadline (condition
  wait), never extend helper timeouts to mask flakes.

## Mock services (no real data) — repo standard

`FUNDVISTA_UPSTREAM_MODE=mock` (read at import of `backend.main`) swaps the
network edge: every third-party call — OurAirports CSVs, Overpass (airport +
citymap), OSM API, Nominatim, GitHub release index/archive — is served from
deterministic fixtures in `backend/upstream_mock/`, and any request to an
unmocked host **fails closed** with `UpstreamNotMocked` (a `RuntimeError` so
`except httpx.HTTPError` handlers cannot swallow it into a quiet 502). See
`backend/tests/test_upstream_mock.py`.

- When a new third-party dependency is added, add its fixture to
  `backend/upstream_mock/` instead of ever reaching the network.
- Endpoint-level tests that need per-case stubbing use the **in-process
  uvicorn thread** pattern from `backend/tests/test_traffic_api.py`
  (same process ⇒ `monkeypatch` stubs apply to the serving app) and follow
  its autouse `_reset` isolation block.
- Only stub upstream **boundary functions** (e.g.
  `resolve_airport`, `asource_status`, `_load_raw`, `_download_and_extract`)
  — never patch internals deeper, never spin up ad-hoc mock HTTP servers.
- The catch-all proxy's upstreams (`kh.hu`, `erstemarket`, `frankfurter`)
  intentionally have no mock fixtures: proxy integration tests must swap
  `backend.main.http_client` for an `httpx.MockTransport` client.

## Boundary + equivalence-partition rules

Test each input domain with one test per equivalence class (valid classes +
invalid classes) and explicit boundaries `min−1 / min / min+1` and
`max−1 / max / max+1` where the domain allows. Full conventions: the
`test-design` skill. Negative coverage is mandatory for every endpoint.

## Redis content

- The citymap and airports API suites **MUST** assert on what the app writes
  to Redis, not only on `cache_hit` flags. They run against a throwaway
  `redis-server` started by `backend/tests/redis_harness.py` (needs
  `redis-server` on PATH) and read keys with `redis_harness.cached_values`.
- Each test starts with a flushed Redis (`redis_harness.flush()`), so every
  test is cold and independent.
- Other suites keep `REDIS_CLOUD_URL=redis://127.0.0.1:1/0` (memory fallback).

## Baseline commands

```sh
# Full suite (hermetic; no external calls; the API suites start their own redis-server):
PENPLOT_DATA_DIR=/tmp/fv-test REDIS_CLOUD_URL=redis://127.0.0.1:1/0 \
PENPLOT_RATE_LIMIT=100000 PYTHONPATH=. \
backend/.venv/bin/python -m pytest backend/tests/ -q

# Schemathesis smoke (also what CI runs — starts its own mock-mode server):
ops/schemathesis-smoke.sh
```

Readiness probe standard: `curl -s -o /dev/null -w "%{http_code}" …/healthz`
must return 200 before tests run (conftest handles this; the smoke script
re-implements it for CLI use).

## Test-driven fixes (affilio rule)

For a bug fix, **MUST** use TDD: write the regression test BEFORE the code
change; it **MUST** fail pre-fix (RED, verified by running it) and pass
post-fix (GREEN); then re-run the full relevant suite. Pure unit tests
**MUST** mock external services (`httpx.MockTransport`) so they run offline
and deterministically.

## CI (GitHub Actions)

`.github/workflows/backend-tests.yml` runs on backend-affecting paths and
`workflow_dispatch`:

| Job | What |
|---|---|
| `integration` | Full pytest suite, Python 3.13, hermetic env, mocked upstreams |
| `schemathesis` | `ops/schemathesis-smoke.sh`: OpenAPI property fuzzing (`not_a_server_error`, ~10 examples/operation) against the running mock-mode app; junit report uploaded as artifact on failure |

The smoke is `max-failures`-capped and excludes only the catch-all proxy
ительства (no mock upstreams by design). It exists to find whole
request-shape bug classes example-based tests miss — it caught
`normalize_icao` 500 and the subnormal-speed `inf` 500 on day one.
