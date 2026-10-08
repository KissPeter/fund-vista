"""Shared HTTP error envelope (REF-002 Phase 1, fix-once home).

All v1 routers map failures to ``{"error": {"code", "message"}}`` so
clients can switch on ``code``. Previously each router carried its own
byte-identical ``_error`` copy (citymap, airports, jobs) plus a
``headers``-accepting variant in penplot — one helper now serves all
four callers.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse


def error_response(
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    """Build the shared ``{"error": {"code", "message"}}`` response."""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )


__all__ = ["error_response"]
