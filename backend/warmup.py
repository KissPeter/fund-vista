"""Render-cache warmer for the shop's warm-up defaults (fund-vista #1433).

Two triggers replay the shop's warm-up contract against the NAS backend so
the 24 h render cache is hot for the default previews:

* Daily: at 00:05 local time, shortly after midnight and before the render
  cache TTL lapses.
* Startup: once per deploy (the backend version is the key), started in the
  background so application startup is never delayed.

Both triggers MUST stay disabled unless BOTH settings are present:

* ``SHOP_BASE_URL`` - shop origin. The contract is fetched from
  ``<SHOP_BASE_URL>/api/warmup-defaults``.
* ``WARMUP_TARGET_URL`` - NAS backend base URL the items are replayed against.
  It MUST NOT be the FastAPI Cloud fallback.

Optional:

* ``WARMUP_ITEM_TIMEOUT_S`` - per-item timeout in seconds (default 120).
* ``WARMUP_INGEST_TIMEOUT_S`` - per traffic_ingest job timeout in seconds
  (default 3600).

Contract (shop side, issue KissPeter/pen-pixel-shop#1432):
``{generatedAt, items:[...]}`` where an item is either a replay item
``{name, method:"POST", path, body, contentType, assetUrl?}`` or a day-load
item ``{name, kind:"traffic_ingest", icao, date}``.

Replay rules:

* Items run sequentially (concurrency 1) with a per-item timeout. Runs are
  serialised inside one worker by a module lock.
* ``body`` MUST be the exact payload string as the shop produced it. It is
  sent as its UTF-8 bytes without re-serialising, because the render cache key
  includes every parameter byte.
* ``contentType`` ``application/json`` is replayed. ``multipart`` items (PCB)
  are skipped and logged: the contract does not yet specify the multipart
  boundary. ``assetUrl`` is not fetched.
* A ``traffic_ingest`` item gates the traffic replay items that follow it. The
  NAS availability verdict decides: ``ready`` skips the ingest and opens the
  gate; ``available`` runs one ``traffic_ingest`` job on the NAS and waits for
  it (up to ``WARMUP_INGEST_TIMEOUT_S``), then opens the gate; any other verdict
  is retried hourly, up to 12 attempts, then given up and logged. While the gate
  is closed, ``/v1/traffic/`` replay items are skipped and logged.
* A failing item (HTTP error, timeout, malformed item) is logged and skipped.
  It MUST NOT stop the remaining items or raise into startup.
* A failed contract fetch logs a warning and replays nothing.
* With Redis available, a ``SET NX`` claim (``warmup:daily:<date>``,
  ``warmup:startup:<version>``) lets one of the uvicorn workers run each
  trigger. Without Redis every worker runs it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional, Union

import httpx

log = logging.getLogger("fund-vista.warmup")

CONTRACT_PATH = "/api/warmup-defaults"
DAILY_AT = (0, 5)  # local 00:05
DEFAULT_ITEM_TIMEOUT_S = 120.0
DEFAULT_INGEST_TIMEOUT_S = 3600.0
DAILY_CLAIM_TTL_S = 6 * 3600
STARTUP_CLAIM_TTL_S = 3600
SUPPORTED_CONTENT_TYPES = ("application/json",)
TRAFFIC_PATH_PREFIX = "/v1/traffic/"
INGEST_KIND = "traffic_ingest"
AVAILABILITY_ATTEMPTS = 12
AVAILABILITY_INTERVAL_S = 3600.0
JOB_POLL_INTERVAL_S = 5.0

# Serialises warm-up runs inside one worker (startup and daily never overlap).
_RUN_LOCK = asyncio.Lock()

# claim(key, ttl_seconds) -> True when this worker won the trigger.
Claim = Callable[[str, int], Awaitable[bool]]
Sleep = Callable[[float], Awaitable[Any]]


@dataclass(frozen=True)
class WarmupConfig:
    shop_base_url: str
    target_url: str
    backend_version: str
    item_timeout_s: float = DEFAULT_ITEM_TIMEOUT_S
    ingest_timeout_s: float = DEFAULT_INGEST_TIMEOUT_S

    @property
    def enabled(self) -> bool:
        return bool(self.shop_base_url.strip()) and bool(self.target_url.strip())


def contract_url(shop_base_url: str) -> str:
    return shop_base_url.rstrip("/") + CONTRACT_PATH


def seconds_until_daily(now: datetime) -> float:
    """Seconds from ``now`` (local, naive) to the next 00:05."""
    target = now.replace(hour=DAILY_AT[0], minute=DAILY_AT[1], second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


@dataclass(frozen=True)
class _Replay:
    name: str
    path: str
    content_type: str
    body: bytes


@dataclass(frozen=True)
class _Ingest:
    name: str
    icao: str
    date: str


def _validate_item(item: Any) -> Union[_Replay, _Ingest]:
    """Parse one contract item or raise ValueError (the item is then skipped)."""
    if not isinstance(item, dict):
        raise ValueError("item is not an object")
    name = str(item.get("name") or "")
    if item.get("kind") == INGEST_KIND:
        icao, date = item.get("icao"), item.get("date")
        if not isinstance(icao, str) or not icao or not isinstance(date, str) or not date:
            raise ValueError("traffic_ingest needs icao and date")
        return _Ingest(name=name, icao=icao, date=date)
    if item.get("method") != "POST":
        raise ValueError(f"unsupported method {item.get('method')!r}")
    path = item.get("path")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or "://" in path
    ):
        raise ValueError(f"path must be a relative absolute path: {path!r}")
    content_type = item.get("contentType")
    if content_type not in SUPPORTED_CONTENT_TYPES:
        raise ValueError(f"unsupported contentType {content_type!r}")
    body = item.get("body")
    if not isinstance(body, str):
        raise ValueError("body must be the exact payload string")
    return _Replay(name=name, path=path, content_type=content_type, body=body.encode("utf-8"))


async def _availability(http: httpx.AsyncClient, base: str, icao: str, date: str) -> Optional[str]:
    """NAS verdict for one day (ready | available | unavailable | ...), None on error."""
    try:
        resp = await http.get(base + "/v1/traffic/availability",
                              params={"icao": icao, "date": date},
                              timeout=DEFAULT_ITEM_TIMEOUT_S)
        if resp.status_code >= 400:
            log.warning("warmup: availability %s %s -> %d", icao, date, resp.status_code)
            return None
        return str(resp.json().get("status"))
    except Exception as exc:
        log.warning("warmup: availability %s %s failed: %s", icao, date,
                    str(exc) or type(exc).__name__)
        return None


async def _ingest_and_wait(http: httpx.AsyncClient, base: str, item: _Ingest,
                           ingest_timeout_s: float, poll_s: float) -> bool:
    """Run one traffic_ingest job on the NAS and wait for a terminal state."""
    created = await http.post(base + "/v1/jobs", timeout=DEFAULT_ITEM_TIMEOUT_S,
                              json={"type": INGEST_KIND,
                                    "request": {"icao": item.icao, "date": item.date}})
    if created.status_code >= 400:
        log.warning("warmup: ingest %s %s rejected -> %d", item.icao, item.date, created.status_code)
        return False
    created_body = created.json()
    status_url = str(created_body.get("status_url") or "")
    job_id = created_body.get("job_id")
    if not status_url.startswith("/"):
        log.warning("warmup: ingest %s %s: no usable status_url", item.icao, item.date)
        return False
    deadline = time.monotonic() + ingest_timeout_s
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_s)
        job = await http.get(base + status_url, timeout=DEFAULT_ITEM_TIMEOUT_S)
        state = job.json().get("status") if job.status_code < 400 else None
        if state == "done":
            log.info("warmup: ingest %s %s done (job %s)", item.icao, item.date, job_id)
            return True
        if state in ("failed", "cancelled"):
            log.warning("warmup: ingest %s %s %s (job %s)", item.icao, item.date, state, job_id)
            return False
    log.warning("warmup: ingest %s %s timed out after %.0fs, cancelling job %s",
                item.icao, item.date, ingest_timeout_s, job_id)
    try:
        await http.delete(base + f"/v1/jobs/{job_id}", timeout=DEFAULT_ITEM_TIMEOUT_S)
    except Exception:
        pass  # best effort: the NAS reaps orphaned jobs on its own
    return False


async def _ensure_day_ingested(http: httpx.AsyncClient, base: str, item: _Ingest,
                               ingest_timeout_s: float, poll_s: float,
                               interval_s: float, attempts: int, sleep: Sleep) -> bool:
    for attempt in range(1, attempts + 1):
        verdict = await _availability(http, base, item.icao, item.date)
        if verdict == "ready":
            log.info("warmup: %s %s already ingested, skipping ingest", item.icao, item.date)
            return True
        if verdict == "available":
            return await _ingest_and_wait(http, base, item, ingest_timeout_s, poll_s)
        log.info("warmup: %s %s not published yet (verdict=%s), attempt %d/%d",
                 item.icao, item.date, verdict, attempt, attempts)
        if attempt < attempts:
            await sleep(interval_s)
    log.warning("warmup: %s %s not available after %d attempts, giving up",
                item.icao, item.date, attempts)
    return False


async def run_warmup(
    shop_base_url: str,
    target_url: str,
    *,
    item_timeout_s: float = DEFAULT_ITEM_TIMEOUT_S,
    ingest_timeout_s: float = DEFAULT_INGEST_TIMEOUT_S,
    client: Optional[httpx.AsyncClient] = None,
    poll_interval_s: float = JOB_POLL_INTERVAL_S,
    availability_interval_s: float = AVAILABILITY_INTERVAL_S,
    availability_attempts: int = AVAILABILITY_ATTEMPTS,
    sleep: Sleep = asyncio.sleep,
) -> dict:
    """Fetch the contract and replay it. Never raises for item errors."""
    summary = {"total": 0, "ok": 0, "failed": 0, "skipped": 0}
    own_client = client is None
    http = client or httpx.AsyncClient(follow_redirects=False)
    try:
        try:
            resp = await http.get(contract_url(shop_base_url), timeout=item_timeout_s)
            resp.raise_for_status()
            items = resp.json().get("items")
            if not isinstance(items, list):
                raise ValueError("contract has no items list")
        except Exception as exc:
            log.warning("warmup: contract fetch failed, nothing replayed: %s", exc)
            return summary

        base = target_url.rstrip("/")
        gate_open = False  # traffic replays need a ready day (set by the last traffic_ingest)
        for raw in items:
            summary["total"] += 1
            try:
                item = _validate_item(raw)
            except ValueError as exc:
                summary["skipped"] += 1
                log.warning("warmup: skipped item: %s", exc)
                continue

            if isinstance(item, _Ingest):
                try:
                    gate_open = await _ensure_day_ingested(
                        http, base, item, ingest_timeout_s, poll_interval_s,
                        availability_interval_s, availability_attempts, sleep)
                except Exception as exc:  # the gate stays closed, the run goes on
                    gate_open = False
                    log.warning("warmup: ingest %s %s failed: %s", item.icao, item.date,
                                str(exc) or type(exc).__name__)
                continue

            name, path = item.name, item.path
            if path.startswith(TRAFFIC_PATH_PREFIX) and not gate_open:
                summary["skipped"] += 1
                log.warning("warmup: %s %s skipped, day not ingested", name, path)
                continue
            started = time.monotonic()
            try:
                response = await asyncio.wait_for(
                    http.post(
                        base + path,
                        content=item.body,
                        headers={"content-type": item.content_type},
                        timeout=item_timeout_s,
                    ),
                    timeout=item_timeout_s,
                )
                elapsed = time.monotonic() - started
                if response.status_code < 400:
                    summary["ok"] += 1
                    log.info("warmup: %s %s -> %d in %.1fs", name, path, response.status_code, elapsed)
                else:
                    summary["failed"] += 1
                    log.warning("warmup: %s %s -> %d in %.1fs", name, path, response.status_code, elapsed)
            except Exception as exc:  # per-item failure must not stop the loop
                summary["failed"] += 1
                log.warning("warmup: %s %s failed: %s", name, path, str(exc) or type(exc).__name__)
        log.info("warmup: done %s", summary)
        return summary
    finally:
        if own_client:
            await http.aclose()


async def _run_claimed(cfg: WarmupConfig, claim: Optional[Claim], key: str, ttl: int) -> None:
    try:
        if claim is not None and not await claim(key, ttl):
            log.info("warmup: %s already claimed by another worker", key)
            return
        async with _RUN_LOCK:
            await run_warmup(cfg.shop_base_url, cfg.target_url,
                             item_timeout_s=cfg.item_timeout_s,
                             ingest_timeout_s=cfg.ingest_timeout_s)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("warmup: run failed")


async def _daily_loop(cfg: WarmupConfig, claim: Optional[Claim]) -> None:
    while True:
        await asyncio.sleep(seconds_until_daily(datetime.now()))
        day = datetime.now().date().isoformat()
        await _run_claimed(cfg, claim, f"warmup:daily:{day}", DAILY_CLAIM_TTL_S)
        await asyncio.sleep(1)  # never re-fire inside the same minute


async def _startup_once(cfg: WarmupConfig, claim: Optional[Claim]) -> None:
    await _run_claimed(cfg, claim, f"warmup:startup:{cfg.backend_version}", STARTUP_CLAIM_TTL_S)


def start(cfg: WarmupConfig, claim: Optional[Claim] = None) -> list[asyncio.Task]:
    """Start both triggers. Returns no tasks (and does nothing) unless enabled."""
    if not cfg.enabled:
        return []
    log.info("warmup: enabled, target=%s", cfg.target_url)
    return [
        asyncio.create_task(_daily_loop(cfg, claim), name="warmup-daily"),
        asyncio.create_task(_startup_once(cfg, claim), name="warmup-startup"),
    ]
