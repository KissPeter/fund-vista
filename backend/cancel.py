"""Cooperative cancel for CPU-heavy POSTs (render-cancel plan P2).

Client abort (nginx RST / fetch AbortController) only drops the client wait —
the dispatched Overpass + SVG work runs to completion unless handlers poll
for disconnect. This module is the single place for that polling so all five
handlers share one log line, one counter, and one 499 shape:

* ``await check_cancelled(request, endpoint=..., stage=...)`` — async
  checkpoint (FastAPI ``request.is_disconnected()``). Raises 499 on disconnect.
* ``race_cancel(request, coro, ...)`` — race an Overpass ``httpx`` fetch
  against a 200 ms disconnect watcher (httpx has no disconnect signal).
* Sync CPU loops (split/render/vpype stages) accept ``cancelled: () -> bool``
  backed by a ``threading.Event`` that an async watcher sets — a running
  thread finishes its current stage (<= seconds) but no further stage starts.

Cancel never returns a JSON error to the (gone) client in production — the
response is discarded — but the handler raises ``HTTPException(499)`` so
tests and nginx access logs see a distinct, non-5xx status. 499 is logged at
INFO, increments ``penplot_cancelled_total{endpoint,stage}``, and must never
write cache entries or partial ``svg_url`` files.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, TypeVar

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Poll interval for disconnect watchers (matches plan §P2.3/§P2.4).
POLL_S = 0.2

#: In-process counter backing ``penplot_cancelled_total{endpoint,stage}``.
#: (A Prometheus client is not a dependency; export via logs + tests.)
_cancelled_total: dict[tuple[str, str], int] = {}


class ClientCancelled(Exception):
    """Raised inside sync CPU code when the client went away."""

    def __init__(self, endpoint: str = "", stage: str = "") -> None:
        super().__init__(f"client closed request ({endpoint} {stage})".strip())
        self.endpoint = endpoint
        self.stage = stage


def gone_499() -> HTTPException:
    """499 error for a disconnected client (never a 5xx, never alerted)."""
    return HTTPException(status_code=499, detail="client closed request")


def request_id_of(request: Request | None) -> str:
    try:
        if request is not None:
            rid = request.headers.get("x-request-id")
            if rid:
                return rid
    except Exception:
        pass
    return "-"


def client_ip_of(request: Request | None) -> str:
    try:
        if request is not None:
            fwd = request.headers.get("x-forwarded-for")
            if fwd:
                head = fwd.split(",")[0].strip()
                if head:
                    return head
            if request.client is not None:
                return request.client.host
    except Exception:
        pass
    return "unknown"


def inc_cancelled(endpoint: str, stage: str) -> int:
    key = (endpoint, stage)
    _cancelled_total[key] = _cancelled_total.get(key, 0) + 1
    return _cancelled_total[key]


def get_cancelled_total(
    endpoint: str | None = None, stage: str | None = None
) -> int:
    total = 0
    for (ep, st), count in _cancelled_total.items():
        if endpoint is not None and ep != endpoint:
            continue
        if stage is not None and st != stage:
            continue
        total += count
    return total


def reset_cancelled_total() -> None:
    _cancelled_total.clear()


def _log_cancel(
    endpoint: str, stage: str, request: Request | None, started_mono: float
) -> None:
    elapsed_ms = int((time.monotonic() - started_mono) * 1000)
    # request_id already flows (x-request-id); keep it for nginx correlation.
    try:
        fwd = ""
        if request is not None:
            fwd = request.headers.get("x-forwarded-for", "")
    except Exception:
        fwd = ""
    log.info(
        "cancel endpoint=%s stage=%s request_id=%s ip=%s elapsed_ms=%d",
        endpoint,
        stage,
        request_id_of(request),
        fwd or client_ip_of(request),
        elapsed_ms,
    )


async def is_disconnected(request: Request | None) -> bool:
    """True when the client went away. False when unknowable."""
    if request is None:
        return False
    try:
        return bool(await request.is_disconnected())
    except Exception:
        return False


async def check_cancelled(
    request: Request | None,
    *,
    endpoint: str,
    stage: str,
    started_mono: float,
) -> None:
    """Checkpoint: raise 499 (INFO log + counter) when disconnected.

    Call at the four handler checkpoints: after validation+cache, before the
    Overpass fetch / image load, after the fetch / decode before the build,
    and between CPU stages.
    """
    if await is_disconnected(request):
        _log_cancel(endpoint, stage, request, started_mono)
        inc_cancelled(endpoint, stage)
        raise gone_499()


def check_cancel_sync(
    cancelled: Callable[[], bool] | None,
    *,
    endpoint: str = "",
    stage: str = "",
) -> None:
    """Sync checkpoint for code running inside ``to_thread``.

    Raises :class:`ClientCancelled` (mapped to 499 by the handler) so a
    threaded stage loop exits before the next stage starts.
    """
    if cancelled is not None and cancelled():
        raise ClientCancelled(endpoint, stage)


async def race_cancel(
    request: Request | None,
    coro: Coroutine[Any, Any, T],
    *,
    endpoint: str,
    stage: str,
    started_mono: float,
    poll_s: float = POLL_S,
) -> T:
    """Race ``coro`` (Overpass httpx fetch) against disconnect.

    On cancel: cancel the fetch task, INFO-log, increment the counter, raise
    499. The partial response is never cached. ``asyncio.CancelledError`` from
    the outer scope is mapped to 499 the same way.
    """
    task: asyncio.Task[T] = asyncio.create_task(coro)  # type: ignore[arg-type]
    try:
        while not task.done():
            if await is_disconnected(request):
                task.cancel()
                _log_cancel(endpoint, stage, request, started_mono)
                inc_cancelled(endpoint, stage)
                raise gone_499()
            try:
                await asyncio.wait([task], timeout=poll_s)
            except asyncio.CancelledError:
                task.cancel()
                _log_cancel(endpoint, stage, request, started_mono)
                inc_cancelled(endpoint, stage)
                raise gone_499() from None
        return task.result()
    except asyncio.CancelledError:
        try:
            task.cancel()
        except Exception:
            pass
        # Outer cancellation (client disconnect / job cancel) → 499, not 500.
        _log_cancel(endpoint, stage, request, started_mono)
        inc_cancelled(endpoint, stage)
        raise gone_499() from None


def start_disconnect_watcher(
    request: Request | None,
    stop: threading.Event,
    *,
    poll_s: float = POLL_S,
) -> Callable[[], Awaitable[None]]:
    """Return an async ``watch()`` that sets ``stop`` when client disconnects.

    Handlers run ``watch`` concurrently with ``asyncio.to_thread(build, ...)``
    so sync stage loops polling ``stop.is_set`` abort at the next boundary::

        stop = threading.Event()
        watch = start_disconnect_watcher(request, stop)
        watcher = asyncio.create_task(watch())
        try:
            result = await asyncio.to_thread(build, ..., stop.is_set)
        finally:
            stop.set()  # let watcher exit
            watcher.cancel()
    """

    async def watch() -> None:
        try:
            while not stop.is_set():
                if await is_disconnected(request):
                    stop.set()
                    return
                await asyncio.sleep(poll_s)
        except asyncio.CancelledError:
            pass

    return watch


def throw_if_stop_set(
    stop: threading.Event,
    *,
    endpoint: str,
    stage: str,
    request: Request | None = None,
    started_mono: float | None = None,
) -> None:
    """Sync boundary check wired to a watcher event → 499 via ClientCancelled.

    Handlers catch :class:`ClientCancelled`, INFO-log + increment, and raise
    499 — so no cache write and no partial file happen below.
    """
    if stop.is_set():
        if started_mono is not None:
            _log_cancel(endpoint, stage, request, started_mono)
            inc_cancelled(endpoint, stage)
        raise ClientCancelled(endpoint, stage)


def log_and_499(
    *,
    endpoint: str,
    stage: str,
    request: Request | None,
    started_mono: float,
) -> HTTPException:
    """Log + count a sync-side cancel, return the 499 to raise."""
    _log_cancel(endpoint, stage, request, started_mono)
    inc_cancelled(endpoint, stage)
    return gone_499()


__all__ = [
    "POLL_S",
    "ClientCancelled",
    "gone_499",
    "request_id_of",
    "client_ip_of",
    "inc_cancelled",
    "get_cancelled_total",
    "reset_cancelled_total",
    "is_disconnected",
    "check_cancelled",
    "check_cancel_sync",
    "race_cancel",
    "start_disconnect_watcher",
    "throw_if_stop_set",
    "log_and_499",
]
