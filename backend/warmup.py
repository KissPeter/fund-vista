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

Contract (shop side, issue KissPeter/pen-pixel-shop#1432):
``{generatedAt, items:[{name, method:"POST", path, body, contentType, assetUrl?}]}``.

Replay rules:

* Items run sequentially (concurrency 1) with a per-item timeout.
* ``body`` MUST be the exact payload string as the shop produced it. It is
  sent as its UTF-8 bytes without re-serialising, because the render cache key
  includes every parameter byte.
* ``contentType`` ``application/json`` is replayed. ``multipart`` items are
  skipped and logged: the contract does not yet specify the multipart boundary,
  so replaying them would send a body the target cannot parse. ``assetUrl`` is
  not fetched.
* A failing item (HTTP error, timeout, malformed item) is logged and skipped;
  it MUST NOT stop the remaining items or raise into startup.
* A claim callable (Redis ``SET NX``) makes only one worker of the N uvicorn
  workers run each trigger. Without Redis every worker runs it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional, Tuple

import httpx

log = logging.getLogger("fund-vista.warmup")

CONTRACT_PATH = "/api/warmup-defaults"
DAILY_AT = (0, 5)  # local 00:05
DEFAULT_ITEM_TIMEOUT_S = 120.0
DAILY_CLAIM_TTL_S = 6 * 3600
STARTUP_CLAIM_TTL_S = 3600
SUPPORTED_CONTENT_TYPES = ("application/json",)

# claim(key, ttl_seconds) -> True when this worker won the trigger.
Claim = Callable[[str, int], Awaitable[bool]]


@dataclass(frozen=True)
class WarmupConfig:
    shop_base_url: str
    target_url: str
    backend_version: str
    item_timeout_s: float = DEFAULT_ITEM_TIMEOUT_S

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


def _validate_item(item: Any) -> Tuple[str, str, str, bytes]:
    """Return (name, path, content_type, body_bytes) or raise ValueError."""
    if not isinstance(item, dict):
        raise ValueError("item is not an object")
    name = str(item.get("name") or "")
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
    return name, path, content_type, body.encode("utf-8")


async def run_warmup(
    shop_base_url: str,
    target_url: str,
    *,
    item_timeout_s: float = DEFAULT_ITEM_TIMEOUT_S,
    client: Optional[httpx.AsyncClient] = None,
) -> dict:
    """Fetch the contract and replay every supported item. Never raises for item errors."""
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
        for raw in items:
            summary["total"] += 1
            try:
                name, path, content_type, body = _validate_item(raw)
            except ValueError as exc:
                summary["skipped"] += 1
                log.warning("warmup: skipped item: %s", exc)
                continue
            started = time.monotonic()
            try:
                response = await asyncio.wait_for(
                    http.post(
                        base + path,
                        content=body,
                        headers={"content-type": content_type},
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
        await run_warmup(cfg.shop_base_url, cfg.target_url, item_timeout_s=cfg.item_timeout_s)
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
