# CR-006 — pydantic-owned validation + tiny ops tools (issue #64)

- Status: done
- Created: 2026-10-21
- Last updated: 2026-10-21
- Type: change request
- Issue: https://github.com/KissPeter/fund-vista/issues/64
- Decided: input validation lives in pydantic models, and the ops scripts
  call small importable python functions instead of embedding python
  one-liners in shell strings.

## Motivation

Issue #64 asked for two things:

1. **Pydantic models instead of any other type of validation.** The backend
   already validates the v1 bodies with pydantic v2
   (`backend/penplot/schemas.py`, `backend/traffic/schemas.py`), but a few
   places still hand-rolled their checks — most visibly
   `GET /v1/traffic/availability`, which parsed the ICAO and the day by
   hand and raised the errors itself.
2. **No inline python scripts.** The ops scripts carried python one-liners
   inside shell strings (`python3 -c 'import json,sys; ...'`), which are
   unreadable, untestable and impossible to reuse — the same trap the
   affilio repo hit with its JUnit summary snippet.

## Scope

### Pydantic owns the input rules

- `backend/traffic/schemas.py`: new `IcaoQuery` (1-8 characters,
  upper-cased) and `DayQuery` (ISO `YYYY-MM-DD`) models. The
  Hungary-only rule stays a router decision — it has its own error code
  (`unsupported_airport`), not `invalid_params`.
- `backend/traffic/router.py`: the availability endpoint validates through
  the two models and maps a `ValidationError` onto the shared error
  envelope; the check order (ICAO format → Hungary → day format) and every
  status code / `error.code` are unchanged.
- `backend/traffic/router.py`: `os.environ.get("TRAFFIC_GITHUB_TIMEOUT_S")`
  → `settings.github_timeout_s` — the typed settings object that already
  existed for the module.
- `backend/upstream_mock/config.py`: new typed settings for
  `FUNDVISTA_UPSTREAM_MODE`; `upstream_mock.enabled()` reads them instead
  of `os.environ.get`. The mode still follows the environment at call time
  (the settings object reads the environment when it is constructed).

### Tiny tools instead of inline scripts

`backend/tools/` (stdlib only — it must run in the production image and on
a plain host checkout):

| Module | Replaces |
|---|---|
| `backend.tools.json_field` | the JSON one-liners in `ops/traffic-ingest-daily.sh` |
| `backend.tools.http_get` | the `urllib.request` probes inside `docker run` in `ops/deploy-mac.sh` |
| `backend.tools.untar_stream` | the `tarfile` one-liner in the deploy script's data migration |

`untar_stream` extracts with the `data` filter: absolute paths, `..`
traversal, device files and outside links are refused instead of written.

## Definition of Done

- [x] The validation rules live in pydantic models; the routers only map
      the errors.
- [x] The wire contract is unchanged: same status codes, same
      `error.code`/`error.message` values (the traffic API tests are green
      unchanged, plus new boundary tests).
- [x] No `python -c` / heredoc python left in `ops/`, `.github/` or
      `runtests.sh`.
- [x] No `os.environ.get` left in the request path (only the
      `OMP_THREAD_LIMIT`/`PENPLOT_OCR_*` `setdefault` calls, which must run
      before the native libraries load).
- [x] Tests: live-HTTP for the traffic query models
      (`backend/tests/test_traffic_query_models.py`), process-level for the
      tools (`backend/tests/test_tools.py`).
- [x] Full backend suite green: 754 passed, 1 skipped (the Linux-only
      tesserocr-wheel test), 0 failed locally; 755 passed, 0 skipped,
      0 failed on CI (Linux).
- [x] The pre-existing failure
      `test_penplot_drawing.py::test_budget_overrun_returns_no_words_and_a_warning`
      (it resolved a real OCR backend on hosts without tesserocr /
      pytesseract) is fixed in this branch — the test now pins the backend
      resolution.
