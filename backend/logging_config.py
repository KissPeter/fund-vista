"""Process-wide logging setup: every line carries a timestamp.

Container logs without timestamps hide how long the retry/backoff/park
logic actually waited between attempts (observed 2026-11-03: Overpass 429
retries were unreadable). One format for the root logger and for uvicorn's
own handlers, so app, ``upstream.*`` and access lines line up.
"""

from __future__ import annotations

import logging

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def configure_logging(level: str | int = "INFO") -> None:
    """Root logging with timestamps; also re-formats uvicorn's handlers."""
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=LOG_DATEFMT)
    formatter = logging.Formatter(LOG_FORMAT, LOG_DATEFMT)
    # basicConfig is a no-op when handlers already exist (uvicorn installs
    # its own), so apply the format to every existing handler as well.
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(formatter)
    # ``upstream.response`` (backend.http.upstream) already logs every
    # outbound attempt with timing; httpx's own INFO line is a duplicate.
    logging.getLogger("httpx").setLevel(logging.WARNING)


__all__ = ["LOG_DATEFMT", "LOG_FORMAT", "configure_logging"]
