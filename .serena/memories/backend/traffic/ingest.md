Traffic ingest (adsb.lol day archive -> per-airport slices), backend/traffic/.

Pipeline (issue #66, CR-003 + CR-007):
- `asset_assets(day, client)` reads the GitHub release assets with their
  size (the progress total); `asset_urls()` is a wrapper.
- `_parallel_chunks(assets, client, workers, queue_size, on_progress, cancelled)`
  fetches the tar parts concurrently (one thread + one bounded queue per
  part) and yields their bytes in archive order. The serial path is
  `_chunks()`. Switch: `TRAFFIC_INGEST_DOWNLOAD_WORKERS` (0/1 = serial,
  the default) and `TRAFFIC_INGEST_DOWNLOAD_QUEUE`.
- `extract_multi(stream, specs, on_progress=...)` parses the archive:
  `workers > 1` = ProcessPoolExecutor (`TRAFFIC_INGEST_WORKERS`), the
  batches merge in archive order so the result equals the serial pass.
- The job (`backend/jobs/_exec_traffic.py`) publishes
  `{"stage", "done", "total"}` events: download bytes (total = the
  archive size) then parsed members. `_ProgressBridge` carries them from
  the download thread to the event loop; `_poll_progress` writes them into
  the job document every second.
- `backend/jobs/store.py::set_progress` is a compare-and-set (Redis
  WATCH/MULTI): the job document is replaced as a whole, so a plain
  read-modify-write could overwrite a cancel -> a lost cancel would leave
  a 4 GB download running with nobody able to stop it. A cancelling or
  terminal document is never written to.

Measured (production container, 20 000 synthetic members, 30.7 MB):
the parent owns 100 % serial, ~20 % at 4 workers; its cost is the tar walk
(40 % `tarfile.calc_chksums`), the member shipping to the pool is ~3 %.
The parent's floor is the tar walk, not more workers. Reproduce with
`backend/tools/profile_ingest_parent.py` (py-spy needs root on macOS -
run it in the container).
