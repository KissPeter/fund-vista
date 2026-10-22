## What

<!-- one or two sentences: what changed -->

## Why

<!-- the problem this solves; link the issue (Closes #<n>) -->

## Checklist

- [ ] Backend rules: no inline `os.getenv` (settings only through the typed
      settings object), Pydantic v2 on every request/response model.
- [ ] Tests: live-HTTP integration by default (unit only for complex
      algorithms); the backend suite is green — the pre-existing
      environmental failures are listed by name.
- [ ] **`backend/uv.lock` changed? → the shop has to regenerate its licence
      inventory.** The shop's `docs/third-party-inventory.md` embeds the
      digest of this file, and the shop's unit lane compares the committed
      file with a fresh generator run — so the shop's CI goes red until
      someone runs `python3 tools/licenses/inventory.py` in
      `KissPeter/pen-pixel-shop` and commits the result. Say so in the PR
      body. Details: `backend/THIRD_PARTY_NOTICES.md` §D.
- [ ] No secrets in the diff.
