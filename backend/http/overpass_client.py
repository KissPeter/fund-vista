"""The one Overpass client (citymap and airports both delegate here).

Walks the configured public mirrors until one answers a usable payload.
Transport-level resilience (backoff, transient 5xx, connect failures,
timeouts, logging) is the shared smart client's job
(:func:`backend.http.upstream.smart_client`); this module adds only what is
Overpass-specific:

* **429 / 503 are a "stay away" answer.** The mirror is *parked* for the
  server's ``Retry-After`` (clamped) or a default, in
  :data:`backend.http.mirror_health.overpass_mirrors` (Redis-shared, so every
  worker and both fetchers respect it), and the walk moves on to the next
  mirror — never a same-mirror retry.
* **Parked mirrors are skipped.** If nothing else answers and the soonest
  park ends inside the remaining budget the client waits for it once;
  otherwise it raises, and the caller goes to its fallback (OSM Main API) or
  its error path instead of hammering a refusing mirror.
* Other hard failures (blocked 403, exhausted 5xx, no answer) park the
  mirror briefly so a dead instance is not re-paid on every render.
* HTTP 200 with a ``remark`` (truncated answer) or non-JSON is retried on the
  same mirror ``retries`` times, then the walk fails over. 400/404 fail fast
  for that mirror (the query is the problem, not the mirror).
* A wall-clock ``budget_s`` caps the whole walk.

Settings-free (the ``backend.http`` convention): callers pass an
:class:`OverpassConfig`.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from backend.http.mirror_health import MirrorHealth, overpass_mirrors
from backend.http.upstream import retry_after_seconds, smart_client

log = logging.getLogger("backend.overpass")

_BODY_SNIPPET_LEN = 200
# A POST started with less budget than this cannot finish meaningfully.
_BUDGET_FLOOR_S = 0.5
# "Stay away" answers: park the mirror, fail over, never retry in place.
_PARK_STATUSES: frozenset[int] = frozenset({429, 503})
# Failures that say "this mirror is unhealthy" (park briefly), as opposed to
# "this query is bad" (400/404: park nothing).
_UNHEALTHY_STATUSES: frozenset[int] = frozenset({403, 408, 500, 502, 504})


class OverpassError(Exception):
    """All Overpass mirrors failed or answered unusable payloads."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class OverpassConfig:
    urls: tuple[str, ...]
    user_agent: str
    timeout_s: float = 30.0
    connect_timeout_s: float = 5.0
    #: Same-mirror retries (transient 5xx/connect via the smart client;
    #: remark/invalid payload here) before failing over.
    retries: int = 1
    retry_backoff_s: float = 1.0
    #: Wall clock for the whole mirror walk.
    budget_s: float = 60.0
    #: Park length for a 429/503 that carries no ``Retry-After``.
    default_park_s: float = 60.0
    #: Ceiling for a server-hinted park.
    max_park_s: float = 300.0
    #: Park length for a mirror that failed hard (blocked, 5xx, no answer).
    fail_park_s: float = 30.0
    name: str = "overpass"


class _BudgetExhausted(Exception):
    pass


class OverpassClient:
    def __init__(
        self,
        config: OverpassConfig,
        *,
        semaphore: Callable[[], asyncio.Semaphore],
        error_cls: type[OverpassError] = OverpassError,
        mirrors: MirrorHealth = overpass_mirrors,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._cfg = config
        self._semaphore = semaphore
        self._error = error_cls
        self._mirrors = mirrors
        self._http = http

    async def fetch(self, query: str, budget_s: float | None = None) -> list[dict]:
        """Elements for ``query`` from the first mirror that answers."""
        cfg = self._cfg
        own_client = self._http is None
        http = self._http or smart_client(
            cfg.name,
            timeout=cfg.timeout_s,
            connect_timeout=cfg.connect_timeout_s,
            retries=cfg.retries,
            backoff_s=cfg.retry_backoff_s,
        )
        budget = cfg.budget_s if budget_s is None else budget_s
        deadline = time.monotonic() + max(0.0, budget)
        failures: list[str] = []
        done: set[str] = set()  # failed for a non-park reason this call
        try:
            for _round in range(2):
                ready, parked = await self._mirrors.split(cfg.urls)
                for url in ready:
                    if url in done:
                        continue
                    try:
                        elements = await self._try_mirror(http, url, query, deadline, failures)
                    except _BudgetExhausted:
                        failures.append(f"overpass budget ({budget:.0f}s) exhausted")
                        log.warning("overpass.budget exhausted mirror=%s", url)
                        raise self._error("; ".join(failures))
                    if elements is not None:
                        return elements
                    done.add(url)
                # Nothing answered. Parked mirrors are skipped; wait for the
                # soonest one only when that fits in what is left of the budget.
                _, parked = await self._mirrors.split(cfg.urls)
                parked = [(u, s) for u, s in parked if u not in done]
                if not parked:
                    break
                url, wait_s = parked[0]
                remaining = deadline - time.monotonic()
                if _round == 1 or wait_s > remaining - _BUDGET_FLOOR_S:
                    for p_url, p_left in parked:
                        failures.append(f"{p_url} parked for another {p_left:.0f}s (rate limited)")
                    log.warning(
                        "overpass.all_parked soonest=%s wait=%.0fs remaining=%.0fs (not waiting)",
                        url, wait_s, remaining,
                    )
                    break
                log.info("overpass.wait mirror=%s sleeping=%.1fs (parked)", url, wait_s)
                await asyncio.sleep(wait_s + 0.05)
            raise self._error("; ".join(failures) or "no mirrors configured")
        finally:
            if own_client:
                await http.aclose()

    async def _try_mirror(
        self,
        http: httpx.AsyncClient,
        url: str,
        query: str,
        deadline: float,
        failures: list[str],
    ) -> list[dict] | None:
        """Elements, or ``None`` after recording why this mirror failed."""
        cfg = self._cfg
        max_attempts = max(1, cfg.retries + 1)
        for attempt in range(max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= _BUDGET_FLOOR_S:
                raise _BudgetExhausted
            try:
                # Overpass answers 406 to generic HTTP-client UAs
                # (python-httpx/*): identify the app on every POST. The
                # semaphore caps concurrent upstream POSTs per worker (fair
                # use); the budget is re-read after queueing.
                async with self._semaphore():
                    remaining = deadline - time.monotonic()
                    if remaining <= _BUDGET_FLOOR_S:
                        raise _BudgetExhausted
                    async with asyncio.timeout(remaining):
                        resp = await http.post(
                            url,
                            data={"data": query},
                            headers={"User-Agent": cfg.user_agent},
                        )
            except TimeoutError:
                raise _BudgetExhausted from None
            except (httpx.HTTPError, ValueError) as exc:
                failures.append(f"{url}: {_format_exc(exc)}")
                await self._mirrors.park(url, cfg.fail_park_s)
                log.warning(
                    "overpass.fail mirror=%s error=%r parked=%.0fs",
                    url, exc, cfg.fail_park_s,
                )
                return None
            status = resp.status_code
            if status != 200:
                detail = f"{url} answered HTTP {status}"
                snippet = _body_snippet(resp)
                if snippet:
                    detail += f" ({snippet})"
                failures.append(detail)
                if status in _PARK_STATUSES:
                    hinted = retry_after_seconds(resp, cfg.default_park_s)
                    park_s = min(max(hinted or cfg.default_park_s, 1.0), cfg.max_park_s)
                    await self._mirrors.park(url, park_s)
                    log.warning(
                        "overpass.parked mirror=%s http=%d for=%.0fs hint=%s (next mirror)",
                        url, status, park_s,
                        resp.headers.get("retry-after") or "none",
                    )
                elif status in _UNHEALTHY_STATUSES:
                    await self._mirrors.park(url, cfg.fail_park_s)
                    log.warning(
                        "overpass.fail mirror=%s http=%d parked=%.0fs",
                        url, status, cfg.fail_park_s,
                    )
                else:
                    log.warning("overpass.fail mirror=%s http=%d", url, status)
                return None
            try:
                payload = resp.json()
            except ValueError as exc:
                detail = f"{url} returned an invalid payload ({_format_exc(exc)})"
                if attempt + 1 < max_attempts:
                    await self._payload_retry(url, "invalid_payload", attempt, max_attempts)
                    continue
                failures.append(detail)
                log.warning("overpass.fail mirror=%s invalid_payload", url)
                return None
            if not isinstance(payload, dict) or "elements" not in payload:
                failures.append(f"{url} returned an invalid payload")
                log.warning("overpass.fail mirror=%s invalid_payload", url)
                return None
            # HTTP 200 with a partial/empty element list plus a ``remark``
            # ("runtime error: Query timed out", "out of memory") means the
            # mirror gave up mid-query: not a complete answer.
            remark = payload.get("remark")
            if remark:
                detail = f"{url} reported a remark ({str(remark)[:_BODY_SNIPPET_LEN]})"
                if attempt + 1 < max_attempts:
                    await self._payload_retry(url, f"remark={remark!r}", attempt, max_attempts)
                    continue
                failures.append(detail)
                log.warning("overpass.fail mirror=%s remark=%r", url, remark)
                return None
            await self._mirrors.clear(url)
            log.info("overpass.ok mirror=%s elements=%d", url, len(payload["elements"]))
            return payload["elements"]
        return None

    async def _payload_retry(
        self, url: str, why: str, attempt: int, max_attempts: int
    ) -> None:
        delay = _backoff(self._cfg.retry_backoff_s, attempt)
        log.warning(
            "overpass.retry mirror=%s %s attempt=%d/%d wait=%.1fs",
            url, why, attempt + 1, max_attempts, delay,
        )
        await asyncio.sleep(delay)


def _format_exc(exc: BaseException) -> str:
    """Never return an empty string (httpx timeouts often stringify to '')."""
    msg = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {msg}" if msg else f"{name} (no detail)"


def _body_snippet(resp: httpx.Response) -> str:
    """First ~200 chars of an error body for diagnostics (403/504 pages)."""
    try:
        text = resp.text.strip().replace("\n", " ")
    except Exception:
        return ""
    if len(text) > _BODY_SNIPPET_LEN:
        return text[:_BODY_SNIPPET_LEN] + "…"
    return text


def _backoff(base_s: float, attempt: int) -> float:
    """Exponential backoff with a small jitter so mirrors don't thunder."""
    return base_s * (2**attempt) + random.uniform(0, 0.25)


__all__ = ["OverpassClient", "OverpassConfig", "OverpassError"]
