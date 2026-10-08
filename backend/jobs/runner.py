"""Background execution for /v1/jobs (plan P3.2).

Each worker keeps a local ``{job_id: asyncio.Task}`` map for tasks it runs;
Redis is the source of truth so GET/DELETE work from any of the 4 workers.
Cancel is polled every 200 ms (P2 ``POLL_S``): the runner watches both the
local ``threading.Event`` (set by a same-worker DELETE) and the Redis status
``cancelling`` (set by any-worker DELETE or ``cancel_previous`` supersede).
A ``queued`` job deleted before its task starts never runs.

Run path reuses the sync handler bodies refactored to take the parsed body +
a ``cancelled: () -> bool`` (P2 checkpoints poll it instead of the more
expensive ``is_disconnected()`` in background): Overpass races watch it,
vpype loops break on it, partial files are never stored. Max runtime 300 s
(matches nginx 310 s); on timeout the task is killed and the job fails with
``{error:{code:"timeout"}}``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from backend.cancel import ClientCancelled
from backend.jobs import store as _store
from backend.jobs._common import _JobError
from backend.jobs._exec_airports import _exec_airport_import, _exec_airport_render
from backend.jobs._exec_citymap import _exec_citymap_import, _exec_citymap_render
from backend.jobs._exec_convert import _exec_convert
from backend.jobs.schemas import MAX_RUNTIME_S
from backend.penplot.errors import PenPlotError

log = logging.getLogger(__name__)

_tasks: dict[str, asyncio.Task] = {}
_stops: dict[str, threading.Event] = {}


def enqueue(job_id: str) -> None:
    """Start ``run_job`` in this worker (no-op when already running)."""
    existing = _tasks.get(job_id)
    if existing is not None and not existing.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    stop = _stops.setdefault(job_id, threading.Event())
    # A job deleted while queued must never run: check before starting.
    task = loop.create_task(_run_job(job_id, stop))
    _tasks[job_id] = task

    def _done(_t: asyncio.Task) -> None:
        _tasks.pop(job_id, None)

    task.add_done_callback(_done)


def request_cancel_local(job_id: str) -> None:
    """Set the local stop event (same-worker DELETE fast path)."""
    stop = _stops.get(job_id)
    if stop is not None:
        stop.set()


def drop_queued(job_id: str) -> None:
    """If the task hasn't started running, cancel it so it never runs."""

    async def _drop() -> None:
        await asyncio.sleep(0)
        task = _tasks.get(job_id)
        doc = await _store.get_job(job_id)
        if (
            task is not None
            and not task.done()
            and doc is not None
            and doc.get("status") in ("cancelling", "cancelled")
            and doc.get("progress", {}).get("stage") == "queued"
        ):
            task.cancel()

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_drop())
    except RuntimeError:
        pass


def reset_local() -> None:
    """Test helper: forget local tasks/stops (running tasks are cancelled)."""
    for task in list(_tasks.values()):
        try:
            task.cancel()
        except Exception:
            pass
    _tasks.clear()
    _stops.clear()


async def _cancel_poller(job_id: str, stop: threading.Event) -> None:
    try:
        while not stop.is_set():
            try:
                doc = await _store.get_job(job_id)
            except Exception:
                doc = None
            if doc is not None and doc.get("status") in ("cancelling", "cancelled"):
                stop.set()
                return
            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        pass


async def _execute(
    job_type: str, payload: dict, job_id: str, stop: threading.Event
) -> dict:
    """Run the sync-equivalent body. Returns the ``result`` dict verbatim."""
    cancelled = stop.is_set
    if job_type == "citymap_render":
        return await _exec_citymap_render(payload, job_id, stop, cancelled)
    if job_type == "citymap_import":
        return await _exec_citymap_import(payload, job_id, stop, cancelled)
    if job_type == "airport_render":
        return await _exec_airport_render(payload, job_id, stop, cancelled)
    if job_type == "airport_import":
        return await _exec_airport_import(payload, job_id, stop, cancelled)
    if job_type == "convert":
        return await _exec_convert(payload, job_id, stop, cancelled)
    raise ValueError(f"Unknown job type '{job_type}'.")


# Map job-failure exceptions to stored ``error`` envelopes (sync tokens kept).
async def _store_job_exception(job_id: str, exc: BaseException) -> None:
    if isinstance(exc, _JobError):
        await _store.set_error(job_id, exc.code, exc.message)
    elif isinstance(exc, PenPlotError):
        await _store.set_error(job_id, exc.code, exc.message)
    else:
        await _store.set_error(job_id, "processing_failed", "Image processing failed.")


# Main entry: maps _JobError/PenPlotError → failed envelope (sync tokens kept).
async def _run_job(job_id: str, stop: threading.Event) -> None:
    doc = await _store.get_job(job_id)
    if doc is None:
        return
    if doc.get("status") in ("cancelling", "cancelled"):
        await _store.set_cancelled(job_id)
        _stops.pop(job_id, None)
        return
    job_type = doc["type"]
    payload = doc.get("request", {})
    started = time.monotonic()
    await _store.set_status(job_id, "running", progress={"stage": "running", "done": 0, "total": 0})
    poller = asyncio.create_task(_cancel_poller(job_id, stop))
    try:
        try:
            result = await asyncio.wait_for(
                _execute(job_type, payload, job_id, stop),
                timeout=MAX_RUNTIME_S,
            )
        except asyncio.TimeoutError:
            log.warning("jobs.timeout id=%s type=%s", job_id[:12], job_type)
            await _store.set_error(job_id, "timeout", "Job exceeded 300 s and was stopped.")
            return
        except asyncio.CancelledError:
            await _store.set_cancelled(job_id)
            return
        except ClientCancelled:
            await _store.set_cancelled(job_id)
            log.info("jobs.cancel id=%s type=%s", job_id[:12], job_type)
            return
        except Exception as exc:  # noqa: BLE001 — mapped to failed envelope
            if isinstance(exc, asyncio.CancelledError):
                await _store.set_cancelled(job_id)
                return
            await _store_job_exception(job_id, exc)
            log.warning("jobs.failed id=%s type=%s: %r", job_id[:12], job_type, exc)
            return
        cur = await _store.get_job(job_id)
        if cur is not None and cur.get("status") in ("cancelling", "cancelled"):
            await _store.set_cancelled(job_id)
            return
        await _store.set_result(job_id, result)
        log.info(
            "jobs.done id=%s type=%s elapsed_ms=%d",
            job_id[:12], job_type, int((time.monotonic() - started) * 1000),
        )
    finally:
        poller.cancel()
        _stops.pop(job_id, None)


__all__ = [
    "enqueue",
    "request_cancel_local",
    "drop_queued",
    "reset_local",
]
