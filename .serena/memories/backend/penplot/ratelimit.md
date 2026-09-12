## Penplot v1 rate limiting (review C.2.2, resolved 2026-09-12)

CPU-exposed penplot POST endpoints (`/v1/images`, `/v1/convert`) are throttled
per-IP with a fixed-window counter — a port of the affilio project's
`CheapLensThrottlingMiddleware` (Redis `INCR` + `EXPIRE`).

- Module: `backend/penplot/ratelimit.py` (`RateLimiter.consume(ip)` →
  `(allowed, retry_after_s)`; module-level `configure_redis(client)` injects
  the Redis client; degrades once to an in-process fixed-window counter when
  Redis is down or errors — intentional, keeps a public endpoint throttled
  in single-instance v1).
- Wiring: FastAPI route dependency `require_rate_limit` in `router.py` applied
  to the two POST routes; `_client_ip` reads `x-forwarded-for` → socket peer;
  whitelist checked first.
- Response: 429 + nested envelope `{"error":{"code":"rate_limited",...}}`
  (PenPlotError gains optional `retry_after`; `penplot_error_handler` emits
  `Retry-After`, `X-Rate-Limit-Limit`, `X-Rate-Limit-Requests-Left`).
- Config (env): `PENPLOT_RATE_LIMIT` (100), `PENPLOT_RATE_LIMIT_WINDOW_S`
  (60), `PENPLOT_RATE_LIMIT_WHITELIST` (csv).
- main.py startup calls `configure_redis(redis_client)` (or None on failure).
- Tests: `backend/tests/test_penplot_ratelimit.py` (3 tests: real-HTTP 429+
  headers via `limited_server` low-limit fixture, whitelist exemption, memory
  fallback). `conftest.py` pins session server to `PENPLOT_RATE_LIMIT=100000`
  and unreachable `REDIS_CLOUD_URL=redis://127.0.0.1:1/0` for hermeticity.
- Run backend tests: `.venv/bin/python -m pytest backend/tests` (40 passed).