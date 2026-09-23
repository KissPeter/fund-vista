"""Wire schemas for POST /v1/jobs (render-cancel plan P3).

Additive contract — sync ``POST /v1/citymap/render|import``,
``POST /v1/airports/render|import`` and ``POST /v1/convert`` keep working
untouched. Async jobs wrap the same request bodies verbatim:

* ``POST /v1/jobs`` body: ``{type, request, cancel_previous}`` where
  ``request`` is the original sync POST body verbatim.
* ``result`` on ``done`` is byte-shape-identical to today's sync success
  (same keys, same absolute ``svg_url`` rule, same ``warnings``/``stats``).
* ``error`` on ``failed`` reuses today's tokens (``invalid_params`` is
  returned synchronously at enqueue; runtime failures reuse the sync
  envelope codes).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

JobType = Literal[
    "citymap_render",
    "citymap_import",
    "airport_render",
    "airport_import",
    "convert",
]

JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]

#: How long results/failures survive before GET turns 404 (plan §P3.1).
RESULT_TTL_S = 3600
FAILURE_TTL_S = 600
CANCELLED_TTL_S = 3600
QUEUED_TTL_S = 3600

#: Max wall-clock per job (matches nginx 310 s with headroom).
MAX_RUNTIME_S = 300.0

#: Per-IP cap on non-terminal jobs (extra POST → 429 rate_limited).
MAX_NONTERMINAL_PER_IP = 3

#: Per-(IP,type) singleflight set TTL (plan §P3.2).
SUPERSEDE_SET_TTL_S = 600


class JobProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str = ""
    done: int = 0
    total: int = 0


class JobCreateRequest(BaseModel):
    """Enqueue a render/convert. ``request`` is validated synchronously."""

    model_config = ConfigDict(extra="forbid")

    type: JobType
    request: dict[str, Any]
    cancel_previous: bool = True


class JobCreateResponse(BaseModel):
    job_id: str
    status: Literal["queued"] = "queued"
    status_url: str
    cancel_url: str
    poll_after_ms: int = 500


class JobStatusResponse(BaseModel):
    job_id: str
    type: JobType
    status: JobStatus | Literal["cancelling"]
    progress: JobProgress | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class JobDeleteResponse(BaseModel):
    job_id: str
    status: Literal["cancelled"] = "cancelled"


__all__ = [
    "JobType",
    "JobStatus",
    "JobProgress",
    "JobCreateRequest",
    "JobCreateResponse",
    "JobStatusResponse",
    "JobDeleteResponse",
    "RESULT_TTL_S",
    "FAILURE_TTL_S",
    "CANCELLED_TTL_S",
    "QUEUED_TTL_S",
    "MAX_RUNTIME_S",
    "MAX_NONTERMINAL_PER_IP",
    "SUPERSEDE_SET_TTL_S",
]
