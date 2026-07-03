import base64
import hashlib
import json
import os
from datetime import datetime, timedelta
from typing import Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis
from redis.exceptions import RedisError

UPSTREAMS: Dict[str, str] = {
    "api": "https://www.kh.hu",
    "ersteapi": "https://www.erstemarket.hu",
    "fxapi": "https://api.frankfurter.app",
}

CACHEABLE_METHODS = {"GET", "POST"}
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_PREFIX = "fund-vista-proxy"

app = FastAPI(title="Fund Vista Proxy")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:8080").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    redis_client = Redis.from_url(REDIS_URL, decode_responses=False)
    await redis_client.ping()


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
    upstream = UPSTREAMS.get(prefix)
    if upstream is None:
        raise HTTPException(status_code=404, detail="Unknown upstream prefix")

    body = await request.body()
    method = request.method.upper()
    query = request.url.query
    target_url = f"{upstream}/{path}"

    cache_key = _cache_key(prefix, path, query, method, body)
    if redis_client and method in CACHEABLE_METHODS:
        cached = await redis_client.get(cache_key)
        if cached:
            payload = json.loads(cached)
            content = base64.b64decode(payload["body"])
            return Response(
                content=content,
                status_code=payload["status"],
                headers=payload["headers"] | {"x-cache": "HIT"},
            )

    forward_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length", "connection"}
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
