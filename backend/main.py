import base64
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
from backend.penplot.errors import PenPlotError
from backend.penplot.ratelimit import configure_redis
from backend.penplot.router import (
    penplot_error_handler,
    router as penplot_router,
    validation_error_handler,
)
from backend.penplot.ui import ui_router as penplot_ui_router


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
    cors_allow_origins: Annotated[list[str], NoDecode] = ["http://localhost:8080"]
    cors_allow_origin_regex: str = r"(https?://[^/]+:8080|https://[^/]+\.github\.io)"

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


settings = Settings()

logging.basicConfig(level=settings.log_level)

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
    allow_credentials=True,
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
app.include_router(penplot_ui_router)
# City maps next: versioned routes must win over the catch-all proxy below.
app.include_router(citymap_router)
app.include_router(citymap_ui_router)
app.add_exception_handler(PenPlotError, penplot_error_handler)  # type: ignore[arg-type]
app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]

http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
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
    except Exception as exc:
        redis_client = None
        configure_redis(None)
        configure_citymap_redis(None)
        print(f"Redis unavailable, continuing without cache: {exc}")


@app.on_event("shutdown")
async def _shutdown() -> None:
    await http_client.aclose()
    if redis_client:
        await redis_client.aclose()


@app.get("/healthz")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.api_route("/{prefix}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(prefix: str, path: str, request: Request) -> Response:
    global redis_client
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
            if cached:
                payload = json.loads(cached)
                content = base64.b64decode(payload["body"])
                return Response(
                    content=content,
                    status_code=payload["status"],
                    headers={**payload["headers"], "x-cache": "HIT"},
                )
        except RedisError as exc:
            print(f"Redis cache read failed for {cache_key}: {exc}")
            redis_client = None

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
