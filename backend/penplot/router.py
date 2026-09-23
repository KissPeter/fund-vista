"""Thin HTTP glue for /v1. No image math lives here — only:

validate input -> store/pipeline -> schema out, with all failures mapped to
the ``{"error": {"code", "message"}}`` envelope so clients can switch on
``code`` (image_not_found -> silent re-upload, invalid_params -> slider fix).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import threading
import time

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.status import (
    HTTP_404_NOT_FOUND,
    HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    HTTP_422_UNPROCESSABLE_ENTITY,
)

from backend.penplot import imaging
from backend.penplot import tokens as design_tokens
from backend.penplot.config import ALLOWED_RASTER_EXTS, Settings
from backend.penplot.errors import ErrorCode, PenPlotError, image_not_found, rate_limited
from backend.penplot.pipeline import run_convert
from backend.penplot.ratelimit import RateLimiter, get_redis
from backend.penplot.schemas import (
    ConvertRequest,
    ConvertResponse,
    HealthResponse,
    ImageMetaResponse,
    RetainResponse,
    TokenRequest,
    TokenResponse,
    TokenVerifyResponse,
)
from backend.penplot.store import ImageStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["penplot-v1"])

_settings = Settings()
store = ImageStore(
    images_dir=_settings.images_dir,
    results_dir=_settings.results_dir,
    ttl_hours=_settings.image_ttl_hours,
    retained_ttl_hours=_settings.retained_ttl_hours,
)

# CPU-exposed endpoints (spec §5 rate limit, review C.2.2) are throttled per
# IP with a Redis fixed-window counter (in-memory fallback when Redis is down).
_limiter = RateLimiter(
    limit=_settings.rate_limit_requests,
    window_s=_settings.rate_limit_window_s,
)


def get_store() -> ImageStore:
    return store


def get_settings() -> Settings:
    return _settings


def resolve_public_base(request: Request) -> str:
    """Absolute origin for result links (shop work order §3).

    ``PENPLOT_PUBLIC_BASE_URL`` wins when set (production behind a proxy);
    otherwise the request's own origin is used. Always absolute — the shop
    frontend runs on a different origin and cannot use relative links.
    """
    override = _settings.public_base_url.strip().rstrip("/")
    if override:
        return override
    return str(request.base_url).rstrip("/")


def _client_ip(request: Request) -> str:
    # Review D.1.2: the X-Forwarded-For first hop is trusted unconditionally by
    # default. That is correct only behind a proxy that OVERWRITES the header;
    # when PENPLOT_TRUST_FORWARDED_FOR=0 the limiter keys off the socket peer
    # instead, so a directly-exposed instance can't be walked around by
    # rotating XFF values.
    if _settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            head = forwarded.split(",")[0].strip()
            if head:
                return head
    return request.client.host if request.client else "unknown"


async def require_rate_limit(request: Request) -> None:
    """Per-IP fixed-window throttle; 429 + Retry-After when exhausted."""
    client_ip = _client_ip(request)
    if client_ip in _settings.rate_limit_whitelist:
        return
    allowed, retry_after = await _limiter.consume(client_ip)
    if not allowed:
        raise rate_limited(retry_after or 1)


def _error_response(
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )


async def penplot_error_handler(_: Request, exc: PenPlotError) -> JSONResponse:
    headers = {}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)
        headers["X-Rate-Limit-Limit"] = str(_limiter.limit)
        headers["X-Rate-Limit-Requests-Left"] = "0"
    return _error_response(exc.status, exc.code, exc.message, headers=headers)


async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Pydantic failures on /v1/convert mean bad slider values -> invalid_params.
    first = ""
    try:
        errs = exc.errors()
        if errs:
            loc = ".".join(str(p) for p in errs[0].get("loc", ()))
            first = f"{loc}: {errs[0].get('msg', '')}".strip(": ")
    except Exception:
        first = ""
    msg = f"Invalid parameters. {first}".strip()
    return _error_response(HTTP_422_UNPROCESSABLE_ENTITY, ErrorCode.INVALID_PARAMS, msg)


def _image_meta(
    *, image_id: str, ext: str, width: int, height: int,
    is_vector: bool, warnings: list[str], path: str,
) -> ImageMetaResponse:
    return ImageMetaResponse(
        image_id=image_id,
        format=ext,
        width=width,
        height=height,
        is_vector=is_vector,
        warnings=warnings,
        expires_at=store.expires_at_for(path),
    )


def _warnings_for(width: int, height: int, is_vector: bool) -> list[str]:
    if is_vector:
        return []
    dpi = imaging.a4_dpi(width, _settings.a4_width_mm)
    if dpi < _settings.low_res_dpi_threshold:
        return ["low_resolution_for_a4"]
    return []


@router.post("/images", response_model=ImageMetaResponse,
             dependencies=[Depends(require_rate_limit)])
async def upload_image(file: UploadFile = File(...)) -> ImageMetaResponse | JSONResponse:
    data = await file.read()
    if len(data) > _settings.max_upload_bytes:
        return _error_response(
            HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"File exceeds {_settings.max_upload_bytes // (1024 * 1024)} MB limit.",
        )
    ext = imaging.sniff_extension(data, file.filename)
    if ext is None:
        allowed = sorted(set(ALLOWED_RASTER_EXTS) | {"svg"})
        return _error_response(
            HTTP_422_UNPROCESSABLE_ENTITY,
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            f"Unsupported format. Allowed: {allowed}.",
        )
    image_id = store.put_image_bytes(data, ext)
    path = store.find_image(image_id)
    assert path is not None
    try:
        if ext == "svg":
            _, w, h, svg_warnings = imaging.parse_svg_vectors(data)
            width, height = int(round(w)), int(round(h))
            is_vector = True
        else:
            w, h, _fmt = imaging.probe_raster(data)
            width, height, is_vector = w, h, False
    except PenPlotError as exc:
        return _error_response(exc.status, exc.code, exc.message)
    if ext == "svg" and svg_warnings:
        warnings = list(svg_warnings)
    else:
        warnings = _warnings_for(width, height, is_vector)
    log.info(
        "images.upload id=%s fmt=%s %dx%d warnings=%s",
        image_id[:12], ext, width, height, warnings,
    )
    return _image_meta(
        image_id=image_id, ext=ext, width=width, height=height,
        is_vector=is_vector, warnings=warnings, path=path,
    )


@router.get("/images/{image_id}", response_model=ImageMetaResponse,
            dependencies=[Depends(require_rate_limit)])
async def get_image(image_id: str) -> ImageMetaResponse | JSONResponse:
    if len(image_id) != 64 or any(c not in "0123456789abcdef" for c in image_id.lower()):
        return _error_response(404, ErrorCode.IMAGE_NOT_FOUND, "Unknown image id.")
    path = store.find_image(image_id.lower())
    if path is None:
        exc = image_not_found(image_id)
        return _error_response(exc.status, exc.code, exc.message)
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else "png"
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        if ext == "svg":
            _, w, h, svg_warnings = imaging.parse_svg_vectors(data)
            width, height = int(round(w)), int(round(h))
            is_vector = True
        else:
            w, h, _fmt = imaging.probe_raster(data)
            width, height, is_vector = w, h, False
    except PenPlotError as exc:
        return _error_response(exc.status, exc.code, exc.message)
    if ext == "svg" and svg_warnings:
        warnings = list(svg_warnings)
    else:
        warnings = _warnings_for(width, height, is_vector)
    return _image_meta(
        image_id=image_id.lower(), ext=ext, width=width, height=height,
        is_vector=is_vector, warnings=warnings, path=path,
    )


@router.post("/convert", response_model=ConvertResponse,
             dependencies=[Depends(require_rate_limit)])
async def convert(body: ConvertRequest, request: Request) -> ConvertResponse | JSONResponse:
    """Convert an image to plotter SVG (P2 checkpoints, shapes unchanged).

    1. after validation + image lookup, before any CPU work; 2. before image
    decode; 3. after decode, before the vpype build; 4. inside the CPU loop
    (every method pass + every vpype stage via ``cancelled``). Abort → 499
    with no partial ``svg_url`` file written.
    """
    from backend.cancel import (
        ClientCancelled,
        check_cancelled,
        log_and_499,
        start_disconnect_watcher,
    )

    endpoint = "/v1/convert"
    started = time.monotonic()
    image_id = body.image_id.lower()
    await check_cancelled(request, endpoint=endpoint, stage="validation", started_mono=started)
    path = store.find_image(image_id)
    if path is None:
        exc = image_not_found(image_id)
        return _error_response(exc.status, exc.code, exc.message)
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else "png"
    is_vector = ext == "svg"
    await check_cancelled(request, endpoint=endpoint, stage="decode", started_mono=started)
    with open(path, "rb") as fh:
        data = fh.read()
    # Authoritative dims (cheap re-probe; keeps stats honest even if the
    # cached file was replaced between upload and convert).
    try:
        if is_vector:
            _, vw, vh, _svg_warnings = imaging.parse_svg_vectors(data)
            src_w, src_h = float(vw), float(vh)
        else:
            w, h, _fmt = imaging.probe_raster(data)
            src_w, src_h = float(w), float(h)
    except PenPlotError as exc:
        return _error_response(exc.status, exc.code, exc.message)
    await check_cancelled(request, endpoint=endpoint, stage="vpype", started_mono=started)

    stop = threading.Event()
    watch = start_disconnect_watcher(request, stop)
    watcher = asyncio.create_task(watch())
    try:
        # Review D.3.1: run_convert is fully synchronous and GIL-bound (OpenCV,
        # the hatch march, RDP/merge O(n²)); running it inline would stall every
        # route — /healthz, the fund proxy — for the whole convert. Offload to
        # the default thread executor; the function is pure (deterministic,
        # content-addressed), so results are unaffected. The watcher sets
        # ``stop`` on disconnect; the pipeline aborts at the next stage
        # boundary (a running thread finishes its stage, <= seconds, but no
        # further stage starts — P2.4) and no partial file is stored.
        result = await asyncio.to_thread(
            run_convert,
            image_id=image_id, image_bytes=data, is_vector=is_vector,
            src_w=src_w, src_h=src_h, params=body.params, settings=_settings,
            cancelled=stop.is_set,
        )
    except ClientCancelled as exc:
        raise log_and_499(
            endpoint=endpoint, stage=exc.stage or "vpype",
            request=request, started_mono=started,
        )
    except PenPlotError as exc:
        log.warning("convert failed id=%s: %s", image_id[:12], exc.code)
        return _error_response(exc.status, exc.code, exc.message)
    finally:
        stop.set()
        watcher.cancel()
    await check_cancelled(request, endpoint=endpoint, stage="vpype", started_mono=started)

    store.put_result(result.filename, result.svg_text)
    svg_url = f"{resolve_public_base(request)}/v1/results/{result.filename}"
    log.info(
        "convert.ok id=%s method=%s strokes=%d pen_down=%.1fmm",
        image_id[:12], "+".join(body.params.methods or ["hatch"]),
        result.stats.strokes, result.stats.pen_down_mm,
    )
    # Spec §4.3: surface low-res hint on convert too so client/server agree.
    warnings = list(result.warnings)
    if not is_vector and "low_resolution_for_a4" in _warnings_for(int(src_w), int(src_h), False):
        if "low_resolution_for_a4" not in warnings:
            warnings.append("low_resolution_for_a4")
    return ConvertResponse(
        image_id=image_id,
        svg_url=svg_url,
        vpype_command=result.vpype_command,
        stats=result.stats,
        warnings=warnings,
    )


@router.get("/results/{filename}", dependencies=[Depends(require_rate_limit)])
async def get_result(filename: str) -> Response:
    # Filenames are server-generated ({sha}_{hash}_optimized.svg); anything
    # else is a 404, never a path traversal.
    if not filename.endswith("_optimized.svg") or "/" in filename or "\\" in filename:
        return _error_response(
            HTTP_404_NOT_FOUND, "result_not_found", "Unknown result file."
        )
    path = store.result_path(filename)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            svg = fh.read()
    except OSError:
        return _error_response(
            HTTP_404_NOT_FOUND, "result_not_found", "Unknown result file."
        )
    return Response(content=svg, media_type="image/svg+xml")


@router.post("/tokens", response_model=TokenResponse,
             dependencies=[Depends(require_rate_limit)])
async def mint_token(body: TokenRequest) -> TokenResponse | JSONResponse:
    """Mint a signed design token for a known image (shop bridge, §2).

    ``design_id`` reuses the content-addressed ``image_id``; ``sig`` is
    ``hex(HMAC_SHA256(secret, design_id + "|" + exp))`` with a 24 h ``exp``.
    503 ``token_signing_unavailable`` until ``PENPIXEL_HMAC_SECRET`` is set.
    """
    image_id = body.image_id.lower()
    if store.find_image(image_id) is None:
        exc = image_not_found(image_id)
        return _error_response(exc.status, exc.code, exc.message)
    if not _settings.hmac_secret:
        return _error_response(
            503,
            ErrorCode.TOKEN_SIGNING_UNAVAILABLE,
            "Design-token signing is not configured (PENPIXEL_HMAC_SECRET).",
        )
    exp = int(time.time()) + _settings.token_ttl_hours * 3600
    sig = design_tokens.sign_design_token(
        design_id=image_id, exp=exp, secret=_settings.hmac_secret
    )
    log.info("tokens.mint design=%s exp=%d", image_id[:12], exp)
    return TokenResponse(design_id=image_id, exp=exp, sig=sig)


@router.get("/tokens/verify", response_model=TokenVerifyResponse)
async def verify_token(
    design_id: str = Query(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$"),
    exp: int = Query(gt=0),
    sig: str = Query(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$"),
) -> TokenVerifyResponse:
    """Verify a design-token triple (cheap HMAC check, unthrottled).

    Lets Woo/ops confirm a token without minting a new one; ``reason`` is
    ``ok``, ``ok_previous_secret`` (rotation window), or a failure code.
    """
    valid, reason = design_tokens.verify_design_token(
        design_id=design_id.lower(),
        exp=exp,
        sig=sig,
        secret=_settings.hmac_secret,
        previous_secret=_settings.hmac_previous_secret,
    )
    if not valid:
        log.warning(
            "tokens.verify design=%s failed: %s", design_id[:12], reason
        )
    return TokenVerifyResponse(
        design_id=design_id.lower(), exp=exp, valid=valid, reason=reason
    )


@router.post("/images/{image_id}/retain", response_model=RetainResponse,
             dependencies=[Depends(require_rate_limit)])
async def retain_image(image_id: str) -> RetainResponse | JSONResponse:
    """Promote a purchased design to the retained TTL (§4, option a).

    Called after order-paid (server-to-cloud) so the design stays fetchable
    at fulfillment time, days later. Anonymous previews keep the short TTL.
    Idempotent.
    """
    if len(image_id) != 64 or any(c not in "0123456789abcdef" for c in image_id.lower()):
        return _error_response(404, ErrorCode.IMAGE_NOT_FOUND, "Unknown image id.")
    retained = store.retain_image(image_id.lower())
    if retained is None:
        exc = image_not_found(image_id)
        return _error_response(exc.status, exc.code, exc.message)
    _path, expires_at = retained
    log.info("images.retain id=%s expires=%s", image_id[:12], expires_at.isoformat())
    return RetainResponse(
        image_id=image_id.lower(), retained=True, expires_at=expires_at
    )


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse | JSONResponse:
    """Dependency status for uptime monitoring (§6). Anonymous, unthrottled.

    200 ``ok`` when the store is writable and disk is free (Redis reports
    honestly but never fails the check — memory fallback is a supported
    mode); 503 ``down`` when designs cannot be persisted.
    """
    store_writable = True
    try:
        fd, tmp = tempfile.mkstemp(dir=store.images_dir, prefix=".health-")
        with open(fd, "wb") as fh:
            fh.write(b"ok")
        os.remove(tmp)
    except OSError:
        store_writable = False
    try:
        disk_free_mb = shutil.disk_usage(store.images_dir).free / (1024 * 1024)
    except OSError:
        disk_free_mb = 0.0
    redis_client = get_redis()
    if redis_client is None:
        redis_state = "unconfigured"
    else:
        try:
            await asyncio.wait_for(redis_client.ping(), timeout=2.0)
            redis_state = "reachable"
        except Exception:
            redis_state = "unreachable"
    if not store_writable or disk_free_mb < 100.0:
        status = "down"
    elif redis_state == "unreachable":
        status = "degraded"
    else:
        status = "ok"
    body: HealthResponse = HealthResponse(
        status=status,
        redis=redis_state,
        disk_free_mb=round(disk_free_mb, 1),
        store_writable=store_writable,
    )
    if status == "down":
        return JSONResponse(status_code=503, content=body.model_dump(mode="json"))
    return body
