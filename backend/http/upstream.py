"""One "smart" outbound HTTP client for every third-party call.

Every external call (Overpass, OSM API, Nominatim, OurAirports, ...) goes
through :func:`smart_client` so retry, backoff, timeouts and logging behave
the same everywhere and are fixed once:

* **Retries** come from ``httpx-retries`` (``Retry`` + ``RetryTransport``):
  exponential backoff with jitter, a status allow-list, ``Retry-After``
  support. By default only transient server errors (408/500/502/504) and
  *connect* failures are retried on the same host. ``429``/``503`` are NOT
  in the list: a rate-limit answer must not be retried against the host that
  just refused us (that deepens the block) — the caller reads the hint with
  :func:`retry_after_seconds` and parks the host or fails over. Read
  timeouts are not retried either: the host did not answer, and a repeat
  only spends the budget twice.
* **Timeouts**: separate connect and read timeouts (a blackholed host fails
  in seconds, not after the full read timeout).
* **Logging**: every attempt is one ``upstream.response`` /
  ``upstream.error`` line on ``fund-vista.http.upstream`` carrying the
  client name, host, status and elapsed time. With timestamps on the log
  format (:mod:`backend.logging_config`) the wait between attempts is
  readable straight off the log.
* **TLS**: the readable-CA guard of :mod:`backend.http.tls`, and the
  ``FUNDVISTA_UPSTREAM_MODE=mock`` fixtures, both keep working.

The module is settings-free (the ``backend.http`` convention): callers pass
their own timeouts/retries.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable

import httpx
from httpx_retries import Retry, RetryTransport

from backend.http import tls

log = logging.getLogger("fund-vista.http.upstream")

#: Transient server-side failures worth one more try on the same host.
RETRY_STATUSES: tuple[int, ...] = (408, 500, 502, 504)
#: Transport errors retried on the same host: the connection never opened.
RETRY_EXCEPTIONS: tuple[type[Exception], ...] = (httpx.ConnectError, httpx.ConnectTimeout)
#: Methods retried. Overpass queries are POSTs but idempotent reads, so POST
#: is allowed here; callers with non-idempotent POSTs must not use this client.
RETRY_METHODS: tuple[str, ...] = ("GET", "HEAD", "OPTIONS", "POST")

_PARSER = Retry(total=0)


def retry_after_seconds(
    response: httpx.Response, default: float | None = None
) -> float | None:
    """Seconds the server asked us to wait (``Retry-After``), else ``default``.

    Accepts both forms of the header (delta-seconds and HTTP-date) via
    ``httpx-retries``' own parser. Never raises.
    """
    raw = response.headers.get("retry-after")
    if raw:
        try:
            return _PARSER.parse_retry_after(raw)
        except ValueError:
            pass
    return default


class _LoggingTransport(httpx.AsyncBaseTransport):
    """Log every attempt (inside the retry layer, so retries show up)."""

    def __init__(self, inner: httpx.AsyncBaseTransport, name: str) -> None:
        self._inner = inner
        self._name = name

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        started = time.monotonic()
        try:
            response = await self._inner.handle_async_request(request)
        except Exception as exc:
            log.warning(
                "upstream.error name=%s method=%s host=%s error=%s after=%.1fs",
                self._name, request.method, request.url.host,
                type(exc).__name__, time.monotonic() - started,
            )
            raise
        hint = response.headers.get("retry-after")
        log.log(
            logging.INFO if response.status_code < 400 else logging.WARNING,
            "upstream.response name=%s method=%s host=%s status=%d after=%.1fs%s",
            self._name, request.method, request.url.host, response.status_code,
            time.monotonic() - started,
            f" retry_after={hint}" if hint else "",
        )
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def smart_client(
    name: str,
    *,
    timeout: float,
    connect_timeout: float | None = None,
    retries: int = 1,
    backoff_s: float = 1.0,
    retry_statuses: Iterable[int] = RETRY_STATUSES,
    retry_exceptions: Iterable[type[Exception]] = RETRY_EXCEPTIONS,
    allowed_methods: Iterable[str] = RETRY_METHODS,
    **client_kwargs,
) -> httpx.AsyncClient:
    """Build the shared ``httpx.AsyncClient`` for upstream ``name``.

    ``retries`` extra same-host attempts on ``retry_statuses`` /
    ``retry_exceptions`` with ``backoff_s * 2**attempt`` backoff.
    ``Retry-After`` is honoured on the statuses that are retried, capped at
    ``timeout``-scale waits by ``httpx-retries``' ``max_backoff_wait``.
    """
    # Imported here: the mock package is optional at runtime.
    from backend.upstream_mock import enabled, handle, installed

    inner: httpx.AsyncBaseTransport = (
        httpx.MockTransport(handle) if enabled() or installed() else tls.async_transport()
    )
    retry = Retry(
        total=max(0, retries),
        backoff_factor=backoff_s,
        status_forcelist=tuple(retry_statuses) or (504,),
        retry_on_exceptions=tuple(retry_exceptions),
        allowed_methods=tuple(allowed_methods),
        respect_retry_after_header=True,
        max_backoff_wait=max(timeout, backoff_s),
    )
    transport = RetryTransport(transport=_LoggingTransport(inner, name), retry=retry)
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(
            timeout, connect=timeout if connect_timeout is None else connect_timeout
        ),
        **client_kwargs,
    )


__all__ = [
    "RETRY_EXCEPTIONS",
    "RETRY_METHODS",
    "RETRY_STATUSES",
    "retry_after_seconds",
    "smart_client",
]
