"""Async job API: POST /v1/jobs + GET/DELETE /v1/jobs/{id} (plan P3).

Additive — sync paths are untouched. Contract:

* ``POST /v1/jobs`` → ``202 {job_id, status:"queued", status_url,
  cancel_url, poll_after_ms:500}``. ``request`` is validated synchronously
  with the existing pydantic schemas: ``422 {error:{code:"invalid_params"}}``
  immediately, no job created. Enqueue-only so ``<100ms``.
* ``GET /v1/jobs/{id}`` → ``200 {job_id, type, status, progress?, result?,
  error?}``; ``404 {error:{code:"job_not_found"}}`` on unknown/expired id.
* ``DELETE /v1/jobs/{id}`` → ``200 {job_id, status:"cancelled"}``
  (idempotent; a ``done`` job's artifacts expire, a ``queued|running`` job
  is cancelled without running further).

Server mechanics (single container, 4 workers): Redis is the source of truth
(``job:{id}`` JSON + ``jobs:pending`` list + ``ip:{ip}:{type}`` singleflight
sets); each worker keeps a local ``{job_id: asyncio.Task}`` map for tasks it
runs. Cancel works cross-worker because runners poll the Redis status every
200 ms (plus ``PUBLISH job:cancel:{id}`` for wake-up) — a lost TCP disconnect
(tunnel, fallback) still cancels via ``DELETE`` or auto-supersede.
"""

from __future__ import annotations

import logging
import secrets
import time

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from backend.jobs import runner as _runner
from backend.jobs import store as _store
from backend.jobs.schemas import (
    MAX_NONTERMINAL_PER_IP,
    JobCreateRequest,
    JobCreateResponse,
    JobDeleteResponse,
    JobStatusResponse,
    JobType,
)
from backend.penplot.router import require_rate_limit

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/jobs", tags=["jobs-v1"])


def _error(status: int, code: str, message: str, headers: dict | None = None):
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )


def _client_ip(request: Request) -> str:
    try:
        from backend.penplot.router import _settings as _penplot_settings

        if _penplot_settings.trust_forwarded_for:
            fwd = request.headers.get("x-forwarded-for")
            if fwd:
                head = fwd.split(",")[0].strip()
                if head:
                    return head
    except Exception:
        pass
    try:
        if request.client is not None:
            return request.client.host
    except Exception:
        pass
    return "unknown"


def _validate_job_request(job_type: JobType, payload: dict) -> str | None:
    """Validate ``request`` with the sync schema. None when valid, else message."""
    try:
        if job_type in ("citymap_render", "citymap_import"):
            from backend.citymap.schemas import RenderRequest

            RenderRequest(**payload)
        elif job_type in ("airport_render", "airport_import"):
            from backend.airports.schemas import RenderRequest

            RenderRequest(**payload)
        elif job_type == "convert":
            from backend.penplot.schemas import ConvertRequest

            ConvertRequest(**payload)
        else:
            return f"Unknown job type '{job_type}'."
        return None
    except ValidationError as exc:
        try:
            errs = exc.errors()
            first = ""
            if errs:
                loc = ".".join(str(p) for p in errs[0].get("loc", ()))
                first = f"{loc}: {errs[0].get('msg', '')}".strip(": ")
            return f"Invalid parameters. {first}".strip()
        except Exception:
            return "Invalid parameters."
    except Exception as exc:
        return f"Invalid parameters: {exc}"


@router.post("", response_model=JobCreateResponse, status_code=202,
             dependencies=[Depends(require_rate_limit)])
async def create_job(body: JobCreateRequest, request: Request, response: Response):
    """Enqueue only (<100 ms): validate → job_id → Redis → 202 + background task."""
    t0 = time.perf_counter()
    msg = _validate_job_request(body.type, body.request)
    if msg is not None:
        return _error(422, "invalid_params", msg)
    client_ip = _client_ip(request)

    nonterminal = await _store.count_nonterminal_by_ip(client_ip)
    if nonterminal >= MAX_NONTERMINAL_PER_IP:
        return _error(
            429, "rate_limited", "Too many requests. Retry after 60s.",
            headers={"Retry-After": "60"},
        )

    job_id = secrets.token_hex(16)
    superseded: list[str] = []
    if body.cancel_previous:
        try:
            old_ids = await _store.list_nonterminal_by_ip_type(client_ip, body.type)
        except Exception:
            old_ids = []
        for old_id in old_ids:
            try:
                await _store.request_cancel(old_id)
                _runner.request_cancel_local(old_id)
                superseded.append(old_id)
            except Exception:
                continue

    await _store.create_job(
        job_id, body.type, body.request, client_ip,
        superseded=superseded,
    )
    # This worker runs it; other workers see it in Redis and can GET/DELETE.
    try:
        _runner.enqueue(job_id)
    except Exception as exc:
        log.warning("jobs.enqueue failed id=%s: %r", job_id[:12], exc)

    if superseded:
        response.headers["X-Penplot-Superseded"] = ",".join(superseded)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    log.info(
        "jobs.enqueue id=%s type=%s ip=%s superseded=%d ms=%.1f",
        job_id[:12], body.type, client_ip, len(superseded), elapsed_ms,
    )
    return JobCreateResponse(
        job_id=job_id,
        status="queued",
        status_url=f"/v1/jobs/{job_id}",
        cancel_url=f"/v1/jobs/{job_id}",
        poll_after_ms=500,
    )


@router.get("/{job_id}", response_model=JobStatusResponse,
            dependencies=[Depends(require_rate_limit)])
async def get_job(job_id: str):
    """Poll a job. Unknown/expired id → 404 job_not_found."""
    doc = await _store.get_job(job_id)
    if doc is None:
        return _error(404, "job_not_found", f"Unknown or expired job '{job_id}'.")
    status = doc.get("status", "queued")
    # Internal "cancelling" is a transient instant of "cancelled" for pollers.
    if status == "cancelling":
        status = "cancelled"
    progress = doc.get("progress")
    return JobStatusResponse(
        job_id=job_id,
        type=doc["type"],
        status=status,  # type: ignore[arg-type]
        progress=progress,  # type: ignore[arg-type]
        result=doc.get("result"),
        error=doc.get("error"),
    )


@router.delete("/{job_id}", response_model=JobDeleteResponse,
               dependencies=[Depends(require_rate_limit)])
async def delete_job(job_id: str):
    """Cancel a job (idempotent). Queued jobs never run; running jobs are
    killed, partial files deleted, status → cancelled. Done jobs expire."""
    doc = await _store.get_job(job_id)
    if doc is None:
        return _error(404, "job_not_found", f"Unknown or expired job '{job_id}'.")
    status = doc.get("status")
    if status in ("queued", "running", "cancelling"):
        await _store.request_cancel(job_id)
        _runner.request_cancel_local(job_id)
        # If it was still queued locally, drop it without running.
        _runner.drop_queued(job_id)
        log.info("jobs.cancel id=%s was=%s", job_id[:12], status)
    elif status == "done":
        # Mark expired + best-effort artifact cleanup; still idempotent.
        try:
            await _store.expire_job(job_id)
        except Exception:
            pass
        log.info("jobs.cancel id=%s was=done (expired)", job_id[:12])
    return JobDeleteResponse(job_id=job_id, status="cancelled")


__all__ = ["router"]
