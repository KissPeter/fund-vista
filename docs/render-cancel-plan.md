# Aborted render still burns backend CPU — fix plan

Date: 2026-09-23
Status: P2 + P3 IMPLEMENTED (backend @ `7e064f7`; P0/P1 LANDED in shop `1.28` @ `1cb6d18` — statuses below updated, P0/P1 detail lives in the shop copy)
Source: `penplot.linuxadm.hu2.har` + code walkthrough
Implementation: `backend/cancel.py`, `backend/jobs/` (`schemas|store|router|runner.py`),
  checkpoints in `backend/citymap/router.py`, `backend/airports/router.py`,
  `backend/penplot/router.py`; cancellable CPU loops in
  `backend/citymap/overpass.py`, `backend/citymap/render.py`,
  `backend/airports/overpass.py`, `backend/penplot/pipeline.py`.
Tests: `backend/tests/test_cancel.py` (P2 unit/integration, HAR bodies),
  `backend/tests/test_jobs_contract.py` (P3 contract). Full suite: 279 passed.
  P2/P3 sections below are standalone — no shop repo, NAS doc, or HAR file
  needed beyond the bodies quoted in §Appendix.

## Observation

- `[23]` `POST /v1/citymap/render` `07:49:35.083Z` → `net::ERR_ABORTED` after `6765ms` (all `blocked`, `wait:0`, 0 bytes).
- `[24]` `POST /v1/citymap/render` `07:49:41.848Z` (new `bbox`) → `200` in `3790ms` (`x-envoy-upstream-service-time:2895`, `overpass_cache_hit` but `cache_hit:false`).
- `[28]` `POST /v1/convert` → `200` in `66727ms` (66 s blocked POST).

Frontend aborts superseded renders (`src/components/custom/useCitymapRender.ts:55`, `src/lib/useFlight.ts:18`, `src/lib/penplot.api.ts:158-159`), but abort only drops the client wait. The dispatched Overpass + SVG work runs to completion; the response is discarded by the sequence guard (`useCitymapRender.ts:75`).

## Root cause

Topology (`docs/nas-fundvista.md:8-10`): `browser → nginx → NAS:8100 reverse tunnel (fundvista:nas, 4 workers, 3 CPU) → @fundvista_cloud fallback`.

1. Prod: nothing checks client disconnect server-side (`fund-vista` repo, separate from shop) — no `request.is_disconnected()` / task cancel, so nginx RST does not stop the render.
2. Dev/e2e: `src/lib/v1proxy.ts:49-60` never forwards `request.signal` to the upstream `AbortController` (only the 310 s timeout aborts).

With 4 NAS workers, two overlapping renders + a 66 s convert contend directly.

## Plan

### P0 — fire fewer full renders (frontend only) — LANDED in shop `1.28`

- `src/components/custom/useCitymapRender.ts:106-120`: render on map `moveend`/idle only; keep `1200/900/400ms` debounce as fallback.
- Skip tiny deltas: if bbox change < threshold (~50 m) and layers unchanged, don't refire.
- Round bbox to 3 decimals before `POST` so backend `bbox_snapped` actually yields `cache_hit:true`.
- Draft preview `width:400-500` while interacting; full `width:1000` only on settle / attach (`cityRenderBody` in `src/lib/penplot.api.ts:220-227`).

### P1 — propagate abort through proxy + nginx — LANDED in shop `1.28` (proxy part; nginx check is ops-side)

- `src/lib/v1proxy.ts:49-60`: wire `request.signal → controller.abort()` (one-line fix; dev/e2e parity).
- VPS nginx `/etc/nginx/sites-enabled/penplot.linuxadm.hu` (`docs/nas-fundvista.md:22-24`): verify `proxy_ignore_client_abort off` (default) so client RST closes the upstream connection; keep `310s` read/send timeouts.

### P2 — cooperative cancel in fund-vista (real CPU saving) — STANDALONE, IMPLEMENTED

Backend-only. No shop change needed except HAR verification. All five
CPU-heavy POSTs share one pattern (`backend/cancel.py`): 4 checkpoints,
Overpass httpx race, thread kill rules, 499 + `penplot_cancelled_total`.
Success/error JSON shapes unchanged (below); cancel never returns JSON to
the (gone) client — the handler raises 499 (INFO log, never a 5xx alert,
visible in nginx access log as 499). Cancelled runs write no cache entries
and no partial `svg_url` files.

Repo: this repo (`backend/`). Redis optional (degrades to memory).

#### P2.1 Scope (exact endpoints — full contract, no other doc needed)

Apply to all CPU-heavy POSTs (same pattern, five handlers):

- `POST /v1/citymap/render` — body `{bbox:{south,west,north,east}, layers:[...], min_path_len_m, width}` (HAR `[23]/[24]`, bodies in §Appendix). Success: `{city, display_name, bbox, layers, path_counts, raw_counts, svg_url, attribution, warnings[], cache_hit}`.
- `POST /v1/citymap/import` — same body shape; success adds `{image_id}` (no `svg_url`).
- `POST /v1/convert` — body `{image_id (sha256 hex), params:{methods, threshold, blur_radius, contrast, brightness, remove_background, strip_hatch_px, hatch_pitch_mm, hatch_angle_deg, contour_simplify, centerline_prune_px, curve_smooth, linemerge_tolerance_mm, linesimplify_tolerance_mm, linesort, reloop_tolerance_mm, page:{size,orientation,margin_mm,padding_mm,frame,frame_radius_mm}, pen:{draw_speed_mm_s,travel_speed_mm_s,pen_lift_s}, label:{...}, line_color, background}}` (HAR `[28]`). Success: `{image_id, svg_url, vpype_command, stats, warnings[]}`. Runs up to ~66 s.
- Same for `POST /v1/airports/render|import` — body `{icao, radius_m, min_path_len_m, width, zoom, layers?, taxiway_labels}`; render success `{icao, name, municipality, iso_country, runways[], frequencies[], rotation_deg, path_counts, raw_counts, svg_url, warnings[], cache_hit}`; import success `{image_id, ...}` (no `svg_url`).

Do NOT change success/error JSON shapes. Error envelope stays `{error:{code,message}}` with existing tokens (`rate_limited`, `invalid_params`, `payload_too_large`, `image_too_large`, `unsupported_media_type`, `bad_image`) — cancel never returns a JSON error to the (gone) client.

#### P2.2 Checkpoints (all handlers)

Add `request: Request` to each handler signature and insert these awaits (FastAPI `request.is_disconnected()` is async):

1. After validation + cache lookup, before any network/CPU work.
2. Immediately before the Overpass `httpx` call (render/import) or image load (convert).
3. Immediately after the Overpass call / image decode, before SVG/vpype build.
4. Inside the CPU loop: every chunk — every N ways (render, e.g. 2000), every vpype stage (`linemerge → linesimplify → linesort → reloop`), every contour pass (convert).

Sketch (adapt names to the repo's actual functions):

```python
from fastapi import Request, HTTPException

async def _cancelled(request: Request) -> bool:
    return await request.is_disconnected()

@router.post("/v1/citymap/render")
async def citymap_render(body: RenderBody, request: Request):
    if await _cancelled(request):
        raise _gone()  # 499, logged, no work started
    cached = cache_get(snapped_key(body))
    if cached: return cached
    if await _cancelled(request):
        raise _gone()
    overpass = await fetch_overpass_cancellable(body, request)  # §P2.3
    if await _cancelled(request):
        raise _gone()
    svg = await build_svg_cancellable(overpass, body, request)  # checks inside loop
    return svg

def _gone():
    return HTTPException(status_code=499, detail="client closed request")
```

`499` must be logged at INFO (not ERROR), must not increment 5xx alerts, and must appear in nginx access log as `499` (proves propagation). Never write partial `svg_url`/cache entries on cancel.

#### P2.3 Cancelling the Overpass fetch

`httpx` has no disconnect-signal parameter — race the fetch against a watcher:

```python
async def fetch_overpass_cancellable(body, request):
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        fetch = asyncio.create_task(client.post(OVERPASS_URL, data=query(body)))
        try:
            while not fetch.done():
                if await request.is_disconnected():
                    fetch.cancel()
                    raise _gone()
                await asyncio.wait([fetch], timeout=0.2)
            return fetch.result()
        except asyncio.CancelledError:
            fetch.cancel()
            raise _gone()
```

On cancel: `fetch.cancel()`, close client, do not cache the partial response.

#### P2.4 Cancelling CPU/subprocess work

- If vpype runs in-process: break the stage loop on `await _cancelled(request)` between stages; for threaded stages (`asyncio.to_thread` / `run_in_threadpool`) check before dispatching each stage — a running thread finishes its stage (≤ seconds) but no further stage starts.
- If vpype runs as subprocess (`Popen`): keep the handle; watcher task polls `request.is_disconnected()` every 200 ms and calls `proc.terminate()` → `proc.kill()` after 2 s grace. Delete partial output files.
- Convert image decode (`cv2`, `numpy`, 4 workers / 3 CPU on NAS): check before decode and before each method pass (`contour`, `centerline`, `hatch`, `flow`).

#### P2.5 Logging + metrics (required for testing)

- Log line on cancel: `cancel endpoint=/v1/citymap/render stage=<validation|overpass|svg_build|vpype:linesort|...> request_id=<x-request-id> ip=<forwarded-for> elapsed_ms=<n>`. `x-request-id` already flows (`HAR [24]` headers); keep it.
- Counter: `penplot_cancelled_total{endpoint,stage}` (+ existing latency histogram untouched).
- No cache poisoning: cancelled runs must not set `cache_hit` entries nor `overpass` snippet cache.

#### P2.6 Test plan (standalone — implemented in `backend/tests/test_cancel.py`)

Unit (pytest, no network) — run: `.venv/bin/python -m pytest backend/tests/test_cancel.py -q`:

- Mock `Request.is_disconnected` → `True` at each checkpoint; assert: Overpass client never called (checkpoint 1/2), SVG builder never called (checkpoint 3), loop exits before next stage (checkpoint 4), no cache write, `499` raised. Counter `penplot_cancelled_total{endpoint,stage}` incremented.

Integration (in-process, fake slow Overpass):

- Stub Overpass with 5 s delay. `POST /v1/citymap/render` with the HAR `[24]` body (Appendix), abort client after 100 ms. Assert server log `cancel … stage=overpass`, server-side duration ≈ abort time (not 5 s), no `svg_url` file written.
- Same for `/v1/convert` with any valid `image_id` fixture: abort mid-run (pipeline `cancelled` event), assert `ClientCancelled` at the next vpype stage, partial SVG deleted (no `put_result` call).

System (staging, needs only this repo + nginx; NOT executed in CI):

1. Deploy (rebuild image, health check).
2. Fire two overlapping renders (HAR `[23]` + `[24]` bodies in Appendix), abort the first (`curl --max-time 1`): second must still `200` in ~3 s; nginx access log shows `499` for the first `x-request-id`; CPU drops after abort instead of staying pegged.
3. Replay: normal (non-aborted) render/convert responses byte-shape-identical to today (same keys, `svg_url` absolute per `PENPLOT_PUBLIC_BASE_URL`, same error tokens on 422/429/413).

Acceptance: aborted requests stop within ~1 s of disconnect at the current stage boundary; non-aborted traffic unchanged; no new error shapes.

### P3 — job model (STANDALONE, IMPLEMENTED — use when P2 insufficient, e.g. converts still saturate workers)

Goal: no blocked POST longer than ~100 ms; cancel becomes a first-class `DELETE` that kills server work even when the TCP disconnect was lost (tunnel, fallback).

Implementation: `backend/jobs/` — `schemas.py` (contract), `store.py`
(Redis `job:{id}` + `jobs:pending` + `ip:{ip}:{type}` sets, memory fallback),
`router.py` (`POST/GET/DELETE /v1/jobs`), `runner.py` (per-worker
`{job_id: asyncio.Task}` map + 200 ms cross-worker cancel poller reusing P2
checkpoints). Sync paths untouched. Rate-limit via the existing
`require_rate_limit` (100/min/IP, `X-Forwarded-For`, `PENPLOT_TRUST_FORWARDED_FOR`).

#### P3.1 API (additive — sync paths untouched; full contract, no other doc needed)

Keep existing sync `POST` paths untouched. Add:

- `POST /v1/jobs` → `202 {job_id, status:"queued", status_url:"/v1/jobs/{id}", cancel_url:"/v1/jobs/{id}", poll_after_ms:500}`
  body: `{type:"citymap_render"|"citymap_import"|"airport_render"|"airport_import"|"convert", request:<original POST body verbatim — see P2.1 for exact shapes>, cancel_previous:true (default)}`
  Validate `request` synchronously with the existing pydantic schemas: `422 {error:{code:"invalid_params",…}}` immediately, no job created. Rate-limit enforced here too. Must respond in `<100ms` (enqueue only).
- `GET /v1/jobs/{id}` → `200 {job_id, type, status:"queued"|"running"|"done"|"failed"|"cancelled", progress?:{stage, done, total}, result?:<sync success JSON verbatim per P2.1>, error?:{error:{code,message}}}`. `404 {error:{code:"job_not_found"}}` on unknown/expired id. No auth beyond the unguessable id (`secrets.token_hex(16)`, 32 hex chars).
- `DELETE /v1/jobs/{id}` → `200 {job_id, status:"cancelled"}` (idempotent; deleting a `done` job expires it + best-effort artifact cleanup; `queued|running` jobs are cancelled without running further / killed with partial files deleted).

`result` on `done` is byte-shape-identical to the sync success in P2.1 (same `svg_url` absolute-host rule `PENPLOT_PUBLIC_BASE_URL` or request origin, same `warnings`, `stats`). `error` on `failed` reuses the sync tokens (`invalid_params` is sync-422 at enqueue; runtime failures reuse the sync envelope; timeout → `{error:{code:"timeout"}}`). Job TTL: results 1 h, failures 10 min, cancelled 1 h, then `404`.

#### P3.2 Server mechanics (single container, 4 workers — Redis or memory fallback)

- State in Redis (`REDIS_CLOUD_URL`) when reachable, else process-local dict with the same API: key `job:{id}` (JSON `{job_id,type,request,status,progress,result,error,client_ip,created_at,updated_at}`, TTL per §P3.1) + list `jobs:pending` + sets `ip:{ip}:{type}` (TTL 10 min). Workers share nothing in memory across processes, so Redis is the source of truth; each worker keeps a local `{job_id: asyncio.Task}` map (+ `threading.Event` stops) for tasks it runs.
- Enqueue: `POST` does `request validation → per-IP non-terminal count check → job_id = token_hex(16) → write job:{id} {status:queued} → LPUSH jobs:pending + SADD ip:{ip}:{type} → 202`, then `asyncio.create_task(run_job)` in the answering worker.
- Run: `run_job` sets `running`, then `asyncio.wait_for(execute(...), 300)`; `execute` dispatches by `type` to the sync-equivalent body taking the parsed body + `cancelled: () -> bool`. Reuse P2 checkpoints but poll the job stop event (cheaper than `is_disconnected()` in background): Overpass race watches a `_JobRequest` facade whose `is_disconnected()` is the stop event / Redis `cancelling`, vpype/stage loops break on it. A 200 ms poller flips the local stop when any worker's `DELETE`/supersede sets Redis `cancelling` (+ `PUBLISH job:cancel:{id}` wake-up).
- Cancel: `DELETE` sets `cancelling` (+ publish); runner → stop event + `task.cancel()`; `finally:` no partial files stored, status → `cancelled`. If still `queued`, the task is dropped before it runs.
- `cancel_previous:true` (default): on `POST`, list non-terminal `ip:{client_ip}:{type}` ids → same cancel path for each; response header `X-Penplot-Superseded: <old_id,…>`. This is the server-side singleflight that replaces client-only abort.
- Limits: per-IP max 3 non-terminal jobs (extra `POST` → `429 {error:{code:"rate_limited"}}` + `Retry-After: 60`). Max job runtime 300 s (matches 310 s proxy timeouts); on timeout mark `failed {error:{code:"timeout"}}`, kill task.

#### P3.3 Proxy notes (backend contract; no shop code needed to test)

- Interactive clients switch to `POST /v1/jobs` + poll (`500ms → 2s` backoff, 310 s cap) + `DELETE` on supersede/unmount. Sync paths stay as fallback.
- Proxy: no change needed to test (same timeouts; `499` still meaningful for polls). Job execution is backend-local; on poll `502/504` clients re-`POST` rather than following a stale host.

#### P3.4 Test plan (standalone — implemented in `backend/tests/test_jobs_contract.py`)

Run: `.venv/bin/python -m pytest backend/tests/test_jobs_contract.py -q`.
Contract (in-process app, memory job store, fake Overpass/vpype where noted):

- `POST /v1/jobs` invalid body → `422 invalid_params`, no store key.
- Valid `POST` → `202` (enqueue-only; `<100ms` in prod, `<5s` under test load), store `job:{id}` = `queued`; `GET` → `queued`, then `running`, then `done` with `result` matching the sync schema (keys: `svg_url`, `path_counts`/`stats`, `warnings`).
- `DELETE` while `queued` → never runs (no Overpass call). `DELETE` while `running` (gated pipeline stub) → `cancelled`, no partial file, `GET` stays `cancelled`, second `DELETE` still `200 cancelled`.
- `cancel_previous:true`: `POST A` then `POST B` same IP+type → `A = cancelled`, `B` runs; response `B` carries `X-Penplot-Superseded: A`.
- Limits: 4th non-terminal same-IP `POST` → `429 rate_limited` + `Retry-After: 60`.
- TTL: `GET` unknown id → `404 job_not_found`; done-job `GET` after TTL → `404`.
- Cross-check: `result` of async `citymap_render` deep-equals sync `POST /v1/citymap/render` for the HAR `[24]` body (modulo `cache_hit` + cache-hit warning tokens).

System: curl acceptance (no client code needed):

```sh
BASE=http://127.0.0.1:8000  # or https://penplot.linuxadm.hu
# 1. enqueue (never blocks): 202 in <100ms
J=$(curl -s -X POST $BASE/v1/jobs -H 'Content-Type: application/json' \
  -d '{"type":"convert","request":{"image_id":"<64hex>","params":{"methods":["hatch"]}},"cancel_previous":true}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["job_id"])')
# 2. poll loop (500ms → 2s backoff, 310s cap)
while true; do
  S=$(curl -s $BASE/v1/jobs/$J | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])')
  echo "status=$S"; case "$S" in done|failed|cancelled) break;; esac; sleep 1
done
# 3. mid-run cancel from another shell (server-visible even if TCP was lost):
curl -s -X DELETE $BASE/v1/jobs/$J
# 4. singleflight: two overlapping same-IP POSTs → second 202 carries
#    X-Penplot-Superseded: <first-id>; GET <first-id> stays "cancelled".
# 5. slow convert (~66s, HAR [28] shape) completes via polling with no held connection.
```

Acceptance: interactive cancel always server-visible (`DELETE` or auto-supersede); no >100 ms POST; sync shapes preserved inside `result`/`error`.

## Appendix — HAR bodies (standalone fixtures, no .har file needed)

```json
// [24] successful render
{"bbox": {"south": 47.45, "west": 19.02, "north": 47.55, "east": 19.15},
 "layers": ["roads", "buildings"], "min_path_len_m": 10.0, "width": 1000}
// [23] aborted render (superseded by [24])
{"bbox": {"south": 47.46, "west": 19.03, "north": 47.56, "east": 19.16},
 "layers": ["roads", "buildings", "water"], "min_path_len_m": 10.0, "width": 1000}
// [28] convert (66 s blocked POST): {"image_id": "<sha256 hex>", "params": {...}}
```

## Verification

- Backend: `.venv/bin/python -m pytest backend/tests/ -q` green (279 passed incl. P2/P3).
- Logs: aborted id stops (P2: `499` + `penplot_cancelled_total`; P3: `DELETE` cancels task).

## Order

P0 + P1 LANDED in shop `1.28` (draft previews, skip tiny refires, proxy
abort forwarding; shop detail in `pen-pixel-shop/docs/render-cancel-plan.md`).
P2 + P3 implemented here (backend only) — cloud already serves the new
code (`job_not_found` contract observed in prod). NAS tunnel restored
2026-09-23 ~11:13 but the NAS app predates P2/P3 (`/v1/jobs` answers
`Unknown upstream prefix`) — NAS rebuild/redeploy still pending
(requires LAN access to `root@192.168.0.240`), then HAR-level verification.
