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
| `WARMUP_INGEST_TIMEOUT_S` | Optional per `traffic_ingest` job timeout in seconds (default `3600`). |

## Contract

`GET <SHOP_BASE_URL>/api/warmup-defaults` returns
`{generatedAt, items:[...]}`. Two item shapes are recognised:

- Replay item: `{name, method:"POST", path, body, contentType, assetUrl?}`.
- Day-load item: `{name, kind:"traffic_ingest", icao, date}`, placed before
  the traffic render items that need that day.

The shop side is KissPeter/pen-pixel-shop#1432.

## Replay rules

- Items MUST run sequentially (concurrency 1), each with a timeout. Runs are
  serialised inside one worker by a module lock, so a startup run and a daily
  run never overlap.
- `body` MUST be the exact payload string the shop produced. It is sent as its
  UTF-8 bytes without re-serialising, because the render cache key includes
  every parameter byte.
- `path` MUST be a relative absolute path (`/v1/...`). Absolute URLs and
  protocol-relative paths are skipped.
- `contentType` `application/json` is replayed. `multipart` items (PCB) MUST be
  skipped and logged: the contract does not yet specify the multipart boundary.
  `assetUrl` is not fetched.
- A failing item (HTTP error, timeout, malformed item) MUST be logged and
  skipped. It MUST NOT stop the remaining items or raise into startup.
- A failed contract fetch MUST log a warning and replay nothing.
- With Redis, a `SET NX` claim (`warmup:daily:<date>`,
  `warmup:startup:<version>`) lets one uvicorn worker run each trigger.
  Without Redis every worker runs it.

## Traffic day-load gate

A `traffic_ingest` item decides whether the traffic replay items after it run.
The NAS availability verdict `GET /v1/traffic/availability?icao=&date=` decides:

- `ready`: the day is already ingested. The ingest MUST be skipped and the gate
  opens.
- `available`: the archive is published but not ingested. The warmer MUST
  create one job `POST /v1/jobs {type:"traffic_ingest", request:{icao, date}}`,
  poll its status until `done`, and open the gate. A `failed` or `cancelled`
  job, or one still running after `WARMUP_INGEST_TIMEOUT_S`, keeps the gate
  closed. A timed-out job MUST be cancelled with `DELETE /v1/jobs/{id}`.
- Any other verdict (`unavailable`, `out_of_range`, an error): the warmer MUST
  retry hourly, up to 12 attempts in total, then give up and log.

While the gate is closed, every `/v1/traffic/` replay item MUST be skipped and
logged. A `/v1/traffic/` item with no preceding `traffic_ingest` item is
therefore never replayed. Non-traffic items are unaffected by the gate.

## Tests

`backend/tests/test_warmup.py` uses `httpx.MockTransport` as a mocked shop and
a mocked NAS (replay targets, availability, jobs). The retry sleep is injected,
so the hourly retry runs instantly under test. It covers exact body bytes,
failure isolation, sequential execution, skipped items, contract failure, the
gate (ready, available, failed job, timeout with cancel, unpublished day given
up after 12 checks, published on a retry), the 00:05 schedule and the disabled
state.
