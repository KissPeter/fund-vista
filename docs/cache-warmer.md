# Render-cache warmer (issue #1433)

`backend/warmup.py` replays the shop's warm-up defaults against the NAS
backend so the 24 h render cache is hot for the default previews. Two
triggers:

- **Daily** at 00:05 local time, shortly after midnight and before the
  render-cache TTL lapses.
- **Startup**: once per deploy (keyed by `backend_version()`), started in the
  background so startup is never delayed.

## Settings

Both triggers MUST stay disabled unless both URLs are set. With neither set,
no background task is created and tests are unaffected.

| Env var | Meaning |
| --- | --- |
| `SHOP_BASE_URL` | Shop origin. The contract is fetched from `<SHOP_BASE_URL>/api/warmup-defaults`. |
| `WARMUP_TARGET_URL` | NAS backend base URL the items are replayed against. MUST NOT be the FastAPI Cloud fallback. |
| `WARMUP_ITEM_TIMEOUT_S` | Optional per-item timeout in seconds (default `120`). |

## Contract

`GET <SHOP_BASE_URL>/api/warmup-defaults` returns
`{generatedAt, items:[{name, method:"POST", path, body, contentType, assetUrl?}]}`.
The shop side is KissPeter/pen-pixel-shop#1432.

## Replay rules

- Items MUST run sequentially (concurrency 1), each with a timeout.
- `body` MUST be the exact payload string the shop produced. It is sent as its
  UTF-8 bytes without re-serialising, because the render cache key includes
  every parameter byte.
- `path` MUST be a relative absolute path (`/v1/...`). Absolute URLs and
  protocol-relative paths are skipped.
- `contentType` `application/json` is replayed. `multipart` items are skipped
  and logged: the contract does not yet specify the multipart boundary.
  `assetUrl` is not fetched.
- A failing item (HTTP error, timeout, malformed item) is logged and skipped.
  It MUST NOT stop the remaining items or raise into startup.
- The contract fetch failing logs a warning and replays nothing.
- With Redis available, a `SET NX` claim (`warmup:daily:<date>`,
  `warmup:startup:<version>`) lets one of the uvicorn workers run each trigger.
  Without Redis every worker runs it.

## Tests

`backend/tests/test_warmup.py` uses `httpx.MockTransport` as a mocked shop and
mocked NAS. It covers exact body bytes, failure isolation, sequential
execution, skipped items, contract failure, the 00:05 schedule and the
disabled state.
