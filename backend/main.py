import base64
import binascii
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from redis.asyncio import Redis
from redis.exceptions import RedisError

from backend.citymap.cache import configure_citymap_redis
from backend.citymap.router import router as citymap_router
from backend.citymap.ui import ui_router as citymap_ui_router
from backend.airports.cache import configure_airports_redis
from backend.fulfillment.router import router as fulfillment_router
from backend.airports.router import router as airports_router
from backend.airports.ui import ui_router as airports_ui_router
from backend.wordmark.cache import configure_wordmark_redis
from backend.wordmark.router import router as wordmark_router
from backend.jobs.router import router as jobs_router
from backend.jobs.store import configure_jobs_redis
from backend.traffic.cache import configure_traffic_redis
from backend.traffic.router import router as traffic_router
from backend.penplot.compose_sheet import router as compose_sheet_router
from backend.penplot.errors import PenPlotError
from backend.penplot.ratelimit import configure_redis
from backend.http import tls
from backend.penplot.router import (
    penplot_error_handler,
    router as penplot_router,
    validation_error_handler,
)
from backend.penplot.ui import ui_router as penplot_ui_router
from backend.upstream_mock import install_if_enabled as _install_upstream_mock


class Settings(BaseSettings):
    """Fund Vista proxy app settings, built from unprefixed env vars.

    `cors_allow_origins` accepts a comma-separated list from
    `CORS_ALLOW_ORIGINS`; invalid values fail fast at import. An
    explicitly-empty env var counts as unset.
    """

    model_config = SettingsConfigDict(
        env_ignore_empty=True,
        frozen=True,
    )

    log_level: str = "INFO"
    # Field named after its env var on purpose: unprefixed settings read env by
    # field name (REDIS_CLOUD_URL), which tests set to a dead port for hermetic
    # Redis-absent runs.
    redis_cloud_url: str = "redis://localhost:6379/0"
    # NoDecode: skip source-side JSON decode so the raw comma-separated list
    # reaches the split validator (a list is a "complex" env type).
    # Shop work order §1: the TanStack frontend runs on another origin, so
    # the shop domain ships by default; the future own domain (and any other
    # origin) goes through the same CORS_ALLOW_ORIGINS env var.
    cors_allow_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:8080",
        "https://pen-pixel.hu",
    ]
    cors_allow_origin_regex: str = r"(https?://[^/]+:8080|https://[^/]+\.github\.io)"

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            # Dashboards often store lists as JSON (["https://a", "https://b"]).
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, list):
                        return [str(o).strip() for o in parsed if str(o).strip()]
                except json.JSONDecodeError:
                    pass
                text = text.strip("[]")
            return [
                origin.strip().strip("'\"")
                for origin in text.split(",")
                if origin.strip().strip("'\"")
            ]
        return value


settings = Settings()

logging.basicConfig(level=settings.log_level)
logging.getLogger("fund-vista").info(
    "CORS allow_origins=%s allow_origin_regex=%s",
    settings.cors_allow_origins,
    settings.cors_allow_origin_regex,
)

UPSTREAMS: Dict[str, str] = {
    "api": "https://www.kh.hu",
    "ersteapi": "https://www.erstemarket.hu",
    "fxapi": "https://api.frankfurter.dev/v1",
}


CACHEABLE_METHODS = {"GET", "POST"}
CACHE_PREFIX = "fund-vista-proxy:v2"

app = FastAPI(title="Fund Vista Proxy")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_origin_regex=settings.cors_allow_origin_regex,
    # Shop work order §1: no credentials mode — all shop-facing endpoints
    # stay unauthenticated, abuse-controlled by rate limits.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


# v1 pen-plot API first: specific routes must win over the catch-all proxy
# below (Starlette matches in registration order). The /penplot demo page
# rides along here for the same reason.
app.include_router(penplot_router)
# Gallery sheet compose (POST /v1/compose-sheet): same /v1 prefix, same rule.
app.include_router(compose_sheet_router)
app.include_router(penplot_ui_router)
# Async jobs (P3): /v1/jobs must win over the catch-all proxy as well.
app.include_router(jobs_router)
# City maps next: versioned routes must win over the catch-all proxy below.
app.include_router(citymap_router)
app.include_router(citymap_ui_router)
# Airport diagrams last: same versioned-before-catch-all rule.
app.include_router(airports_router)
app.include_router(airports_ui_router)
# Negative-space wordmarks: pure local CPU, same rule.
app.include_router(wordmark_router)
# Airport traffic (Phase 1: availability + ingest job; Mac-only until R2).
app.include_router(traffic_router)
# Fulfillment ink-split (fund-vista #147): one SVG per distinct ink.
app.include_router(fulfillment_router)
app.add_exception_handler(PenPlotError, penplot_error_handler)  # type: ignore[arg-type]
app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]

# FUNDVISTA_UPSTREAM_MODE=mock: serve every third-party call from fixtures
# (no network). Must run before the first httpx client is built.
_install_upstream_mock()

# Guarded client: a corrupted certifi CA bundle would otherwise make the
# constructor raise OSError on every /{prefix}/{path} proxy request.
http_client = tls.async_client(timeout=httpx.Timeout(30.0))
redis_client: Optional[Redis] = None


def _ttl_until_end_of_day() -> int:
    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


def _cache_key(prefix: str, path: str, query: str, method: str, body: bytes) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{CACHE_PREFIX}:{prefix}:{method}:{path}:{query}:{body_hash}"


def _response_headers(headers: httpx.Headers, cache_hit: bool = False) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if "content-type" in headers:
        out["content-type"] = headers["content-type"]
    if "cache-control" in headers:
        out["cache-control"] = headers["cache-control"]
    out["x-cache"] = "HIT" if cache_hit else "MISS"
    return out


@app.on_event("startup")
async def _startup() -> None:
    global redis_client
    try:
        redis_client = Redis.from_url(settings.redis_cloud_url, decode_responses=False)
        await redis_client.ping()
        configure_redis(redis_client)
        configure_citymap_redis(redis_client)
        configure_airports_redis(redis_client)
        configure_wordmark_redis(redis_client)
        configure_jobs_redis(redis_client)
        configure_traffic_redis(redis_client)
    except Exception as exc:
        redis_client = None
        configure_redis(None)
        configure_citymap_redis(None)
        configure_airports_redis(None)
        configure_wordmark_redis(None)
        configure_jobs_redis(None)
        configure_traffic_redis(None)
        print(f"Redis unavailable, continuing without cache: {exc}")


@app.on_event("shutdown")
async def _shutdown() -> None:
    await http_client.aclose()
    if redis_client:
        await redis_client.aclose()


@app.get("/healthz")
async def health() -> Dict[str, str]:
    tls_status = tls.ca_status()
    if tls_status["certifi_readable"] == "yes":
        return {"status": "ok", "outbound_tls": "certifi"}
    fallback = tls_status.get("fallback_bundle")
    if fallback:
        # Fallback keeps outbound HTTPS working; surface it so the image
        # gets rebuilt instead of silently running degraded.
        return {"status": "ok",
                "outbound_tls": f"fallback:{fallback} (certifi unreadable)"}
    return {"status": "degraded",
            "outbound_tls": f"unreadable:{tls_status['certifi_path']}"}


async def _delete_cache_entry(client: "Redis", cache_key: str) -> None:
    """Drop a poisoned cache entry; never let the cleanup itself fail a request."""
    try:
        await client.delete(cache_key)
    except RedisError as exc:
        print(f"Redis cache delete failed for {cache_key}: {exc}")


@app.api_route("/{prefix}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(prefix: str, path: str, request: Request) -> Response:
    upstream = UPSTREAMS.get(prefix)
    if upstream is None:
        raise HTTPException(status_code=404, detail="Unknown upstream prefix")

    body = await request.body()
    method = request.method.upper()
    query = request.url.query
    target_url = f"{upstream}/{path}"

    cache_key = _cache_key(prefix, path, query, method, body)
    if redis_client and method in CACHEABLE_METHODS:
        try:
            cached = await redis_client.get(cache_key)
        except RedisError as exc:
            # A transient Redis failure is a cache miss, not a reason to
            # disable caching for the rest of the process lifetime.
            print(f"Redis cache read failed for {cache_key}: {exc}")
            cached = None
        if cached:
            try:
                payload = json.loads(cached)
                content = base64.b64decode(payload["body"], validate=True)
                response = Response(
                    content=content,
                    status_code=payload["status"],
                    headers={**payload["headers"], "x-cache": "HIT"},
                )
            except (ValueError, binascii.Error, KeyError, TypeError) as exc:
                # A poisoned entry must not turn every request for this key
                # into a 500: drop it and fall through to the upstream fetch.
                print(f"Corrupt cache entry for {cache_key}: {exc}")
                await _delete_cache_entry(redis_client, cache_key)
            else:
                return response

    forward_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower()
        not in {"host", "content-length", "connection", "accept-encoding"}
    }

    try:
        upstream_response = await http_client.request(
            method=method,
            url=target_url,
            params=request.query_params,
            content=body if body else None,
            headers=forward_headers,
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}") from exc

    response_headers = _response_headers(upstream_response.headers, cache_hit=False)
    response = Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
    )

    if redis_client and method in CACHEABLE_METHODS and upstream_response.status_code == 200:
        payload = {
            "status": upstream_response.status_code,
            "headers": _response_headers(upstream_response.headers, cache_hit=False),
            "body": base64.b64encode(upstream_response.content).decode("ascii"),
        }
        try:
            await redis_client.setex(cache_key, _ttl_until_end_of_day(), json.dumps(payload))
        except RedisError as exc:
            print(f"Redis cache write failed for {cache_key}: {exc}")

    return response
