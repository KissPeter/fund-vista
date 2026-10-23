- Status: done
- Created: 2026-10-22
- Last updated: 2026-10-22

# B-002 Local suite shows a false red when no OCR backend is installed

Bug (issue #80): on a dev Mac without the `tesseract-ocr` system binary the
backend suite showed one red test —
`backend/tests/test_penplot_drawing.py::test_budget_overrun_returns_no_words_and_a_warning`
failing with `KeyError: None` at `backend/penplot/drawing.py:250`. The
production code did `OCR_BACKENDS[resolve_backend(settings)][1]()`:
`resolve_backend` returns `None` when no backend is available, so the lookup
raised instead of degrading. CI installs `tesseract-ocr`
(`.github/workflows/backend-tests.yml:57-58`), so the gate was green there and
the red looked like a code regression in the drawing module.

Two independent faults:

1. **Production** — `_read_words` and `_erase_titles` indexed `OCR_BACKENDS`
   with a possibly-`None` backend name. With no backend installed the drawing
   step crashed instead of skipping OCR with a warning.
2. **Test** — the budget-overrun test called the real `_read_words` and
   therefore needed a *real* backend on the host, although the engine was
   mocked. It was host-dependent by accident.

## Phases

- [x] `drawing._reader(settings)`: resolved backend → reader factory, `None`
      when there is no backend (single place that knows the mapping).
- [x] `_read_words`: no backend → `WARNING_OCR_UNAVAILABLE` + no words
      (instead of `KeyError: None`); the slot is still taken first, so the
      queue/cancel behaviour is unchanged.
- [x] `_erase_titles`: no backend → the image is returned untouched.
- [x] Test: the budget-overrun test pins a fake backend
      (`OCR_BACKENDS["fake"]`, `ocr_backend="fake"`) — host-independent.
- [x] Test: `test_no_ocr_backend_skips_the_read_with_a_warning` covers the
      missing-backend path; `test_erase_titles_without_a_backend_leaves_the_image_untouched`
      covers the optional title wipe.
- [x] README: local-run recipe states the `tesseract-ocr` system dependency
      (macOS: `brew install tesseract`).

## Definition of Done

- [x] Code rules: settings-only env, Pydantic v2, no route change
- [x] TDD red → green → refactor
- [x] Negative coverage: missing backend covered by tests
- [x] Full suite green (`backend/.venv/bin/python -m pytest backend/tests/ -q`:
      740 passed, 1 skipped, 0 failed on a Mac without tesseract)
- [x] Docs updated (README local-run recipe, this file, `docs/index.md`)
- [x] Commit per phase, pushed with the PR
