"""Error envelope for the v1 API.

Wire format (stable, versioned under /v1)::

    {"error": {"code": "image_not_found", "message": "..."}}

``code`` is a machine-readable snake_case token (what the spec's clients
switch on); ``message`` is human-readable and safe to display. Internal
tracebacks never leave the server — they go to logs with a request id.
"""

from __future__ import annotations

from typing import Any


class ErrorCode:
    IMAGE_NOT_FOUND = "image_not_found"
    INVALID_PARAMS = "invalid_params"
    PROCESSING_FAILED = "processing_failed"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    BAD_IMAGE = "bad_image"
    RATE_LIMITED = "rate_limited"


class PenPlotError(Exception):
    """Raised inside handlers/pipeline; converted to JSON by the router."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after

    def envelope(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}}


def image_not_found(image_id: str) -> PenPlotError:
    return PenPlotError(
        status=404,
        code=ErrorCode.IMAGE_NOT_FOUND,
        message=f"Image '{image_id}' is not in cache. Re-upload via POST /v1/images.",
    )


def invalid_params(message: str) -> PenPlotError:
    return PenPlotError(
        status=422, code=ErrorCode.INVALID_PARAMS, message=message
    )


def processing_failed(message: str = "Image processing failed.") -> PenPlotError:
    return PenPlotError(
        status=500, code=ErrorCode.PROCESSING_FAILED, message=message
    )


def rate_limited(retry_after: int) -> PenPlotError:
    return PenPlotError(
        status=429,
        code=ErrorCode.RATE_LIMITED,
        message=f"Too many requests. Retry after {retry_after}s.",
        retry_after=retry_after,
    )
