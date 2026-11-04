"""Error envelope for the v1 API.

Wire format (stable, versioned under /v1)::

    {"error": {"code": "image_not_found", "message": "..."}}

``code`` is a machine-readable snake_case token (what the spec's clients
switch on); ``message`` is human-readable and safe to display. Internal
tracebacks never leave the server - they go to logs with a request id.
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
    IMAGE_TOO_LARGE = "image_too_large"
    RATE_LIMITED = "rate_limited"
    TOKEN_SIGNING_UNAVAILABLE = "token_signing_unavailable"
    # compose-sheet, artwork "inline": artwork_unsafe and artwork_not_plottable
    # are input faults (422). sheet_not_plottable is a server fault (500): the
    # input was valid but the composed sheet failed the plottability gate.
    ARTWORK_UNSAFE = "artwork_unsafe"
    ARTWORK_NOT_PLOTTABLE = "artwork_not_plottable"
    SHEET_NOT_PLOTTABLE = "sheet_not_plottable"


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


def sheet_not_plottable() -> PenPlotError:
    """Server fault: the composed sheet failed its plottability gate on valid input."""
    return PenPlotError(
        status=500,
        code=ErrorCode.SHEET_NOT_PLOTTABLE,
        message="The composed sheet failed the plottability check.",
    )


def image_too_large(message: str) -> PenPlotError:
    return PenPlotError(
        status=413, code=ErrorCode.IMAGE_TOO_LARGE, message=message
    )


def rate_limited(retry_after: int) -> PenPlotError:
    return PenPlotError(
        status=429,
        code=ErrorCode.RATE_LIMITED,
        message=f"Too many requests. Retry after {retry_after}s.",
        retry_after=retry_after,
    )
