# Shop work order — implementation record

Date: 2026-09-15
Work order: `pen-pixel-shop/docs/poc/python-service-changes.md` (§§1–8).
Tests: `backend/tests/test_shop_integration.py` (9 tests, live HTTP).
Suite: 213 passed.

## §1 CORS — done

- `backend/main.py`: default `cors_allow_origins` now
  `["http://localhost:8080", "https://penplot.linuxadm.hu"]`; the future
  own domain goes through the existing `CORS_ALLOW_ORIGINS` env var
  (comma-separated, no redeploy-time code change).
- `allow_credentials` flipped `True → False`: no-credentials mode, all
  shop-facing endpoints stay unauthenticated.
- Preflight from the shop origin asserted in tests (OPTIONS + GET carry
  `access-control-allow-origin`).

## §2 Signed design tokens — done (mint endpoint variant)

- New `backend/penplot/tokens.py` (stdlib only): `sign_design_token` /
  `verify_design_token`, constant-time compare, reasons `ok |
  ok_previous_secret | bad_design_id | bad_expiry | expired |
  bad_signature | signing_unconfigured`.
- `POST /v1/tokens {image_id}` (rate-limited) → `{design_id, exp, sig}`;
  404 `image_not_found` for unknown ids, 503
  `token_signing_unavailable` until `PENPIXEL_HMAC_SECRET` is set.
- `GET /v1/tokens/verify?design_id&exp&sig` (cheap, unthrottled) →
  `{design_id, exp, valid, reason}` for Woo/ops checks.
- Rotation: `PENPIXEL_HMAC_SECRET_PREVIOUS` accepted during the 24 h
  dual-accept window (`reason: ok_previous_secret`).
- Canonical algorithm for `docs/backend-contract.md` (shop record):

  ```text
  design_id = image_id (sha256 hex, existing scheme)
  exp       = unix epoch seconds (now + 24 h default, PENPLOT_TOKEN_TTL_HOURS)
  sig       = hex(HMAC_SHA256(secret, design_id + "|" + exp))
  ```

  Test vector logic lives in `test_token_algorithm_matches_contract_vector`
  (independent stdlib reimplementation — copy the values, not the code).

## §3 Absolute result URLs — done

- New `PENPLOT_PUBLIC_BASE_URL` (default `https://fund-vista.fastapicloud.dev`
  in `.env.example`; empty = request Host). `resolve_public_base()` used by
  penplot `POST /v1/convert`, citymap `POST /v1/citymap/render`, airports
  `POST /v1/airports/render`. Tests assert `svg_url` is absolute.

## §4 Retention — done, option (a)

- `ImageStore.retain_image()`: writes a `{id}.{ext}.retained` marker; the
  image then lives under `retained_ttl_hours` (default 2160 = 90 d) instead
  of `image_ttl_hours` (48 h). Marker survives restarts, needs no Redis.
- `POST /v1/images/{image_id}/retain` (rate-limited, idempotent) — the
  call Woo makes after order-paid → `{image_id, retained: true,
  expires_at}`. Anonymous previews keep the short TTL.
- Fulfillment re-fetch path is unchanged (`GET /v1/images/{id}` metadata +
  `/v1/results/*` SVGs); it now works days later for retained designs.

## §5 Rate limits + error codes — done with one deliberate deviation

- Bucket unchanged (per-IP fixed window, env-configurable
  `PENPLOT_RATE_LIMIT*` + whitelist); new state-changing endpoints
  (`POST /v1/tokens`, `POST …/retain`) join the shared bucket, cheap
  GETs (`verify`, `health`) stay unthrottled like `lookup`/geocode.
- `429` already carries `Retry-After` + `X-Rate-Limit-*` (covered by
  `test_penplot_ratelimit.py`).
- **Deviation:** internal `code` names are kept (`image_not_found`,
  `invalid_params`, `rate_limited`, …) instead of renaming to `kp_*`.
  Renaming would break the PoC pages, which switch on codes
  (`image_not_found` → silent re-upload). Mapping for the shop frontend:

  | Python `error.code` (HTTP) | Shop `kp_*` equivalent |
  |---|---|
  | `payload_too_large` (413) | `kp_file_too_large` |
  | `unsupported_media_type` (415) | `kp_unsupported_type` |
  | `bad_image` / `image_too_large` (422/413) | `kp_image_invalid` |
  | `invalid_params` (422) | `kp_invalid_option` / `kp_validation_failed` |
  | `rate_limited` (429 + `Retry-After`) | `kp_rate_limited` |
  | `token_signing_unavailable` (503) | `kp_upstream_unavailable` (retryable) |
  | `image_not_found` / `result_not_found` (404) | re-upload / re-render, then retry |

## §6 Health — done

- `GET /v1/health` (anonymous, unthrottled) →
  `{status: ok|degraded|down, redis: reachable|unreachable|unconfigured,
  disk_free_mb, store_writable}`. 200 unless the store is unwritable or
  disk < 100 MB (503 `down`). Redis `unreachable` → `degraded`, never
  `down` (memory fallback is a supported mode).

## §7 No-pricing — holds (verified, no change)

- `grep price|cost|HUF` over `backend/` hits only comments about plotter
  pen colors and disk-growth cost. No price fields in any schema, no
  catalog ids; paper/pen travel as opaque option strings. Nothing to do.

## §8 Observability — done (minimum)

- Token mint / verify-failure / retain log lines carry the `design=%s`
  (id prefix) correlation key, alongside the existing `image=%s` keys on
  upload/convert/import. A fulfillment failure ("design X won't fetch")
  is greppable by id prefix across all three.

## Acceptance checklist (work-order §Acceptance)

- [x] CORS preflight passes from the shop origin
- [x] Token verifies against the §2 algorithm (contract test vector above)
- [x] All `svg_url`s absolute and publicly fetchable
- [x] Purchased-design retention decided + implemented (§4a)
- [x] 429s carry `Retry-After`; error codes mapped (§5 table)
- [x] `/v1/health` green on all three checks (store, disk, redis)

Remaining shop-side item: copy the §2 algorithm block into
`pen-pixel-shop/docs/backend-contract.md` as the record (that file lives
in the shop repo — not touched here).
