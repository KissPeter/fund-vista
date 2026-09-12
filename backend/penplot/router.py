"""Thin HTTP glue for /v1. No image math lives here — only:

validate input -> store/pipeline -> schema out, with all failures mapped to
the ``{"error": {"code", "message"}}`` envelope so clients can switch on
``code`` (image_not_found -> silent re-upload, invalid_params -> slider fix).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.status import (
    HTTP_404_NOT_FOUND,
    HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    HTTP_422_UNPROCESSABLE_ENTITY,
)

from backend.penplot import imaging
from backend.penplot.config import ALLOWED_RASTER_EXTS, Settings
from backend.penplot.errors import ErrorCode, PenPlotError, image_not_found, rate_limited
from backend.penplot.pipeline import run_convert
from backend.penplot.ratelimit import RateLimiter
from backend.penplot.schemas import (
    ConvertRequest,
    ConvertResponse,
    ImageMetaResponse,
)
from backend.penplot.store import ImageStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["penplot-v1"])

_settings = Settings()
store = ImageStore(
    images_dir=_settings.images_dir,
    results_dir=_settings.results_dir,
    ttl_hours=_settings.image_ttl_hours,
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


def _client_ip(request: Request) -> str:
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
async def upload_image(file: UploadFile = File(...)) -> ImageMetaResponse:
    data = await file.read()
    if len(data) > _settings.max_upload_bytes:
        return _error_response(  # type: ignore[return-value]
            HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"File exceeds {_settings.max_upload_bytes // (1024 * 1024)} MB limit.",
        )
    ext = imaging.sniff_extension(data, file.filename)
    if ext is None:
        allowed = sorted(set(ALLOWED_RASTER_EXTS) | {"svg"})
        return _error_response(  # type: ignore[return-value]
            HTTP_422_UNPROCESSABLE_ENTITY,
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            f"Unsupported format. Allowed: {allowed}.",
        )
    image_id = store.put_image_bytes(data, ext)
    path = store.find_image(image_id)
    assert path is not None
    try:
        if ext == "svg":
            _, w, h = imaging.parse_svg_vectors(data)
            width, height = int(round(w)), int(round(h))
            is_vector = True
        else:
            w, h, _fmt = imaging.probe_raster(data)
            width, height, is_vector = w, h, False
    except PenPlotError as exc:
        return _error_response(exc.status, exc.code, exc.message)  # type: ignore[return-value]
    warnings = _warnings_for(width, height, is_vector)
    log.info(
        "images.upload id=%s fmt=%s %dx%d warnings=%s",
        image_id[:12], ext, width, height, warnings,
    )
    return _image_meta(
        image_id=image_id, ext=ext, width=width, height=height,
        is_vector=is_vector, warnings=warnings, path=path,
    )


@router.get("/images/{image_id}", response_model=ImageMetaResponse)
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
            _, w, h = imaging.parse_svg_vectors(data)
            width, height = int(round(w)), int(round(h))
            is_vector = True
        else:
            w, h, _fmt = imaging.probe_raster(data)
            width, height, is_vector = w, h, False
    except PenPlotError as exc:
        return _error_response(exc.status, exc.code, exc.message)
    return _image_meta(
        image_id=image_id.lower(), ext=ext, width=width, height=height,
        is_vector=is_vector, warnings=_warnings_for(width, height, is_vector),
        path=path,
    )


@router.post("/convert", response_model=ConvertResponse,
             dependencies=[Depends(require_rate_limit)])
async def convert(body: ConvertRequest, request: Request) -> ConvertResponse | JSONResponse:
    image_id = body.image_id.lower()
    path = store.find_image(image_id)
    if path is None:
        exc = image_not_found(image_id)
        return _error_response(exc.status, exc.code, exc.message)
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else "png"
    is_vector = ext == "svg"
    with open(path, "rb") as fh:
        data = fh.read()
    # Authoritative dims (cheap re-probe; keeps stats honest even if the
    # cached file was replaced between upload and convert).
    try:
        if is_vector:
            _, vw, vh = imaging.parse_svg_vectors(data)
            src_w, src_h = float(vw), float(vh)
        else:
            w, h, _fmt = imaging.probe_raster(data)
            src_w, src_h = float(w), float(h)
    except PenPlotError as exc:
        return _error_response(exc.status, exc.code, exc.message)

    try:
        result = run_convert(
            image_id=image_id, image_bytes=data, is_vector=is_vector,
            src_w=src_w, src_h=src_h, params=body.params, settings=_settings,
        )
    except PenPlotError as exc:
        log.warning("convert failed id=%s: %s", image_id[:12], exc.code)
        return _error_response(exc.status, exc.code, exc.message)

    store.put_result(result.filename, result.svg_text)
    base = str(request.base_url).rstrip("/")
    svg_url = f"{base}/v1/results/{result.filename}"
    log.info(
        "convert.ok id=%s method=%s strokes=%d pen_down=%.1fmm",
        image_id[:12], body.params.method,
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


@router.get("/results/{filename}")
async def get_result(filename: str) -> Response:
    # Filenames are server-generated ({sha}_{hash}_optimized.svg); anything
    # else is a 404, never a path traversal.
    if not filename.endswith("_optimized.svg") or "/" in filename or "\\" in filename:
        return _error_response(  # type: ignore[return-value]
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
