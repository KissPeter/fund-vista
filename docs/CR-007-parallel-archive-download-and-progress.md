# CR-007 — Parallel archive download, job progress counters, parent profile

- Status: done
- Created: 2026-10-21
- Last updated: 2026-10-21
- Type: change request
- Note: CR-006 is taken by the open PR #71 (`agent/issue-64`), so the
  numbering skips it — the number is never reused.
- Decided: the tar parts of the day archive are fetched concurrently and
  consumed in order; the `traffic_ingest` job publishes download/parse
  counters into the job document; the parent's share of the pass is
  measured and documented.

## Motivation

Issue #66 closed the multi-airport pass and the parallel parse, and left
three follow-ups (see `docs/CR-003-mac-cd-and-parallel-traffic-ingest.md`):

1. **The download was the last serial stage.** The tar parts
   (`*.tar.aa/.ab/.ac`) were fetched one after the other, so the network
   (not the parse) bounded the pass.
2. **The job was unobservable.** `POST /v1/jobs {type:"traffic_ingest"}`
   reported `progress: {stage: "download"}` with no counters — a 4 GB pass
   looked like a hang.
3. **The parent's share was unknown.** With the parse in a process pool the
   parent still reads the tar and ships member bytes; nobody knew how much
   of the pass it still owned, so nobody knew when it would become the
   bottleneck again.

## Scope

### Parallel download

- `backend/traffic/ingest.py`
  - `asset_assets(day, client)` — the release assets with their **size**
    (from the release metadata), so the progress total is real. The old
    `asset_urls()` is now a thin wrapper over it.
  - `_parallel_chunks(assets, client, workers, queue_size, on_progress,
    cancelled)` — one thread per part, one **bounded** queue per part; the
    consumer drains the queues in archive order, so the byte stream is
    identical to the serial one. Memory ceiling:
    `parts * queue_size * 1 MB`.
  - `_chunks(urls, client, on_progress, cancelled, total)` — the serial
    path, unchanged in behaviour, now with the same progress events.
  - The first failing part raises its exception in the consumer thread
    (an HTTP error is never swallowed into a short stream).
- `backend/traffic/config.py` — `ingest_download_workers` (0/1 = serial,
  the default) and `ingest_download_queue` (buffered 1 MB chunks per part).

### Progress counters

- `backend/traffic/ingest.py` — `extract_multi(..., on_progress=...)` and
  the download helpers publish `{"stage", "done", "total"}` events:
  `stage="download"` with bytes (total = the archive size from the
  release metadata), then `stage="parse"` with the parsed member count.
- `backend/jobs/_exec_traffic.py` — `_ProgressBridge` (a thread-safe
  snapshot) carries the events from the download thread to the event loop;
  `_poll_progress` copies them into the job document every second. The
  job's `progress` field therefore carries real counters instead of only a
  stage name.
- `backend/jobs/store.py` — `set_progress` became a **compare-and-set**.
  The job document is replaced as a whole, so the old read-modify-write
  could overwrite a cancel that landed in between — a lost cancel would
  leave a 4 GB download running with nobody able to stop it. The Redis
  path now uses WATCH/MULTI (the write is aborted when the document
  changed since the read) and never writes to a cancelling or terminal
  document. The in-process fallback has no await point between the read
  and the write, so it is atomic there.

### Parent profile

- `backend/tools/profile_ingest_parent.py` — builds a synthetic archive
  with the adsb.lol member layout, runs the pass, and reports the parent's
  CPU share (cgroup counter) plus the parent's own stack samples
  (py-spy).

## Measured

Container (the production image), 20 000 synthetic members, 30.7 MB:

| workers | wall | parent CPU | cgroup CPU | parent share | efficiency |
|---|---|---|---|---|---|
| 0 (serial) | 4.85 s | 4.85 s | 4.85 s | 100 % | 1.00 |
| 1 | 4.91 s | 4.91 s | 4.91 s | 100 % | 1.00 |
| 2 | 3.00 s | 1.19 s | 5.97 s | 19.9 % | 1.99 |
| 4 | 2.06 s | 1.33 s | 6.56 s | 20.2 % | 3.18 |
| 8 | 2.06 s | 1.49 s | 7.08 s | 21.0 % | 3.43 |

py-spy (100 Hz, 60 000 members, 4 workers), the parent's hot frames
during the extraction phase:

| share | frame |
|---|---|
| 40 % | `tarfile.calc_chksums` (header + payload checksum) |
| 14 % | `tarfile.extractfile` (member read) |
| 11 % | `tarfile.read` |
| 66 % | in `tarfile.__iter__` (the tar walk itself) |
| 3 % | `ProcessPoolExecutor.submit` / `result` (the IPC to the workers) |

**Reading:** the parent's cost is the **tar walk**, not the shipping of
member bytes to the workers (the IPC is ~3 % of its samples). The
checksum verification alone is 40 % of the parent — `tarfile` verifies
every header and payload checksum, and the archive's members are never
needed again once shipped. So the parent's floor is the tar walk, and the
next lever is not more workers but a cheaper tar read (e.g. skipping the
checksum verification), which is out of scope here.

The scaling flattens after 4 workers (the container's CPU quota is 5),
and the parent's 1.3 s of CPU over a 2.06 s wall means the parent is
already ~63 % busy at 4 workers — it will be the next bottleneck if the
quota grows.

## Explicitly unchanged

- The public job API: `POST /v1/jobs` / `GET /v1/jobs/{id}` keep their
  shape. `progress` keeps the `{stage, done, total}` contract — only the
  values become meaningful.
- The serial download path (`workers = 0`) is the default, so a deployment
  that does not set the new env vars behaves exactly as before.
- The multi-airport pass and the process-pool parse (issue #66's core
  change) are untouched.

## DoD

- [x] The parallel download yields byte-identical output to the serial
      download, in archive order, and the parts really overlap in time —
      `backend/tests/test_traffic_download.py` (real HTTP against a local
      socket server).
- [x] Negative coverage: a failing part raises, a cancelled job stops the
      download, the progress events are monotonic and carry the real total.
- [x] The job document carries the counters — live-server test in
      `backend/tests/test_traffic_api.py`.
- [x] The progress write never resurrects a cancelled job (the compare-and-set
      in `backend/jobs/store.py`) — `backend/tests/test_jobs_progress.py`
      (the Redis part runs when `REDIS_TEST_URL` is set; verified against
      the dev container's `fundvista-redis`).
- [x] Full backend suite green (751 passed, 2 skipped; the one failure in
      `test_penplot_drawing.py` pre-dates this work and fails on a clean
      tree too).
- [x] The parent's share of the pass is measured and documented (table
      above) with a reproducible tool
      (`backend/tools/profile_ingest_parent.py`).
- [x] Settings only through the typed settings object; no inline
      `os.getenv`; Pydantic v2 unchanged on the wire.

## Follow-ups

- The tar walk is the parent's floor now (40 % checksum verification) —
  see the "Reading" above.
- The download counters only reach the job document; the shop-side UI does
  not render them yet (the UI lives in pen-pixel-shop).
