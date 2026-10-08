"""Shared scaffolding for background job execution (REF-002 Phase 3).

Fix-once home for the helpers every ``_exec_*`` handler duplicated:
the :class:`_JobRequest` facade, the sync cancel checkpoint, the
``_JobError`` envelope mapping, and the public-base fallback. Pure
moves from :mod:`backend.jobs.runner` — identical behavior.
"""

from __future__ import annotations

import json
import threading

from fastapi import Request

from backend.cancel import ClientCancelled
from backend.jobs import store as _store


class _JobRequest(Request):
    """Minimal Request facade so P2 ``race_cancel`` works for background jobs.

    A real :class:`Request` subclass (not a duck-typed stand-in), so the
    router delegates accept it without ``type: ignore`` — ``scope`` is a
    stub, and ``is_disconnected()`` is True once the job is cancelled
    (local event or Redis ``cancelling``/``cancelled``), letting Overpass
    fetches abort the same way an aborted sync POST does. ``headers``
    carries no x-request-id; cancel logs use the job id as request_id via
    the endpoint label.
    """

    def __init__(self, job_id: str, stop: threading.Event) -> None:
        super().__init__(
            scope={
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/",
                "query_string": b"",
                "headers": [],
                "client": None,
                "server": None,
            }
        )
        self._job_id = job_id
        self._stop = stop

    async def is_disconnected(self) -> bool:  # noqa: D102
        if self._stop.is_set():
            return True
        try:
            doc = await _store.get_job(self._job_id)
        except Exception:
            return False
        return doc is not None and doc.get("status") in ("cancelling", "cancelled")


def _raise_if_cancelled(stop: threading.Event, endpoint: str, stage: str) -> None:
    if stop.is_set():
        raise ClientCancelled(endpoint, stage)


def _public_base() -> str:
    try:
        from backend.penplot.router import _settings as _s

        override = _s.public_base_url.strip().rstrip("/")
        if override:
            return override
    except Exception:
        pass
    return "http://localhost"


class _JobError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _job_err(resp) -> _JobError:
    try:
        body = resp.body.decode() if hasattr(resp, "body") else "{}"
        data = json.loads(body)
        err = data.get("error", {})
        return _JobError(resp.status_code, err.get("code", "processing_failed"),
                         err.get("message", "Job failed."))
    except Exception:
        return _JobError(500, "processing_failed", "Job failed.")


__all__ = [
    "_JobError",
    "_JobRequest",
    "_job_err",
    "_public_base",
    "_raise_if_cancelled",
]
