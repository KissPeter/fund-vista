"""Thin HTTP glue for /v1/wordmark. No wordmark math lives here — only:

text -> font outlines -> boolean dot cuts -> schema out, with all failures
mapped to the ``{"error": {"code", "message"}}`` envelope (the globally
registered penplot handlers already cover ``PenPlotError`` and
``RequestValidationError``).

Expensive endpoints (render/import/results) share the penplot per-IP
rate-limit bucket. Rendering is local CPU (no upstream), so the only
failure modes are validation (422) and processing (500-class, via the
shared handlers). The build runs off the event loop; abort checkpoints
mirror the airports pair (validation+cache, pre/post build) — there is no
long upstream fetch or pollable CPU loop here, so no disconnect watcher.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from backend.cancel import check_cancelled
from backend.wordmark.cache import (
    KEY_PREFIX,
    cache_get,
    cache_get_many,
    cache_set_many,
    wordmark_cache_key,
)
from backend.wordmark.geometry import geometry_source_version, render_wordmark
from backend.wordmark.schemas import (
    ImportResponse,
    RenderRequest,
    RenderResponse,
)
from backend.penplot import imaging
from backend.penplot.errors import ErrorCode, PenPlotError
from backend.penplot.router import require_rate_limit
from backend.penplot.router import resolve_public_base
from backend.penplot.router import store as penplot_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/wordmark", tags=["wordmark-v1"])


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}
    )


def _render_keys(body: RenderRequest) -> tuple[str, str]:
    """``(svg key, metadata key)`` for one wordmark.

    Same shape as the airports pair: the metadata sidecar holds the counts
    and applied cuts the response reports, so a cache hit never re-renders.
    """
    parts = (
        f"text={body.text}",
        f"font={body.font}",
        f"mode={body.mode}",
        f"tracking={body.tracking}",
        f"gap={body.gap}",
        f"cuts={';'.join(f'{c.junction}:{c.y}:{c.r}' for c in body.cuts) if body.cuts else 'auto'}",
        f"width={body.width}",
        f"v={geometry_source_version()}",
    )
    return wordmark_cache_key("svg", *parts), wordmark_cache_key("meta", *parts)


async def _load_cached_render(
    svg_key: str, meta_key: str, warnings: list[str]
) -> tuple[str, dict[str, int], dict[str, int], list[dict]] | None:
    """Cached SVG plus its metadata, or None when either is missing.

    Both are required: the response cannot be built from the document
    alone, and re-deriving the metadata costs a full render. A hit on the
    SVG with no sidecar is therefore treated as a miss.
    """
    found = await cache_get_many([svg_key, meta_key])
    svg_text, meta_json = found.get(svg_key), found.get(meta_key)
    if svg_text is None or meta_json is None:
        return None
    try:
        meta = json.loads(meta_json)
        result = (
            svg_text, meta["path_counts"], meta["raw_counts"], meta["cuts_applied"],
        )
    except (ValueError, KeyError):
        return None
    warnings.append("svg_cache_hit")
    return result


async def _store_render(
    svg_key: str, meta_key: str, svg_text: str,
    path_counts: dict[str, int], raw_counts: dict[str, int],
    cuts_applied: list[dict],
) -> None:
    """Store the SVG and its metadata sidecar in one pipelined write."""
    await cache_set_many({
        svg_key: svg_text,
        meta_key: json.dumps({
            "path_counts": path_counts,
            "raw_counts": raw_counts,
            "cuts_applied": cuts_applied,
        }),
    })


async def _render_uncached(
    body: RenderRequest,
) -> tuple[str, dict[str, int], dict[str, int], list, list[str]]:
    """Build the wordmark off the event loop (CPU-bound boolean ops)."""
    try:
        return await asyncio.to_thread(
            render_wordmark,
            body.text, body.font, body.mode, body.tracking,
            body.gap, body.cuts, body.width,
        )
    except PenPlotError:
        raise
    except Exception as exc:
        log.exception("wordmark.render failed text=%r", body.text)
        raise PenPlotError(
            status=500, code=ErrorCode.PROCESSING_FAILED,
            message=f"Wordmark render failed: {exc}",
        ) from exc


async def _render_or_cached(
    body: RenderRequest, request: Request | None, endpoint: str,
    started: float, warnings: list[str],
) -> tuple[str, dict[str, int], dict[str, int], list, bool]:
    """Shared render/import body: cache hit or fresh build + store.

    Returns (svg, path_counts, raw_counts, cuts_applied, cache_hit).
    Shared (not duplicated) so render and import can never drift.
    """
    # The UI fires /render and then /import with the same payload on every
    # control change, so the import below is almost always the wordmark the
    # preview just rendered.
    svg_key, meta_key = _render_keys(body)
    cached = await _load_cached_render(svg_key, meta_key, warnings)
    if cached is not None:
        svg_text, path_counts, raw_counts, cuts_applied = cached
        return svg_text, path_counts, raw_counts, cuts_applied, True
    await check_cancelled(request, endpoint=endpoint, stage="validation", started_mono=started)
    svg_text, path_counts, raw_counts, cuts_applied, build_warnings = \
        await _render_uncached(body)
    warnings.extend(build_warnings)
    await check_cancelled(request, endpoint=endpoint, stage="svg_build", started_mono=started)
    await _store_render(
        svg_key, meta_key, svg_text, path_counts, raw_counts,
        [c.model_dump() for c in cuts_applied],
    )
    return svg_text, path_counts, raw_counts, cuts_applied, False


@router.post("/render", response_model=RenderResponse,
             dependencies=[Depends(require_rate_limit)])
async def render(body: RenderRequest, request: Request) -> RenderResponse:
    """Render a negative-space wordmark SVG (preview)."""
    endpoint = "/v1/wordmark/render"
    started = time.monotonic()
    warnings: list[str] = []
    svg_text, path_counts, raw_counts, cuts_applied, cache_hit = \
        await _render_or_cached(body, request, endpoint, started, warnings)

    base = resolve_public_base(request)
    svg_key, _ = _render_keys(body)
    token = svg_key.rsplit(":", 1)[-1]
    log.info(
        "wordmark.render text=%r mode=%s cuts=%d cache_hit=%s",
        body.text, body.mode, len(cuts_applied), cache_hit,
    )
    return RenderResponse(
        text=body.text,
        font=body.font,
        mode=body.mode,
        cuts_applied=cuts_applied,  # type: ignore[arg-type]
        path_counts=path_counts,
        raw_counts=raw_counts,
        svg_url=f"{base}/v1/wordmark/results/{token}",
        warnings=warnings,
        cache_hit=cache_hit,
    )


@router.post("/import", response_model=ImportResponse,
             dependencies=[Depends(require_rate_limit)])
async def import_wordmark(body: RenderRequest, request: Request) -> ImportResponse | JSONResponse:
    """Render a wordmark and register it as a penplot image.

    Returns ``image_id`` — convert it with ``POST /v1/convert`` exactly
    like an uploaded SVG (vector branch: every method applies — Contour
    traces directly, Centerline/Hatch/Flow rasterize first so threshold
    and tone shape them; pen/page/label/display all apply).
    """
    endpoint = "/v1/wordmark/import"
    started = time.monotonic()
    warnings: list[str] = []
    svg_text, path_counts, raw_counts, cuts_applied, _ = \
        await _render_or_cached(body, request, endpoint, started, warnings)

    try:
        imaging.parse_svg_vectors(svg_text.encode("utf-8"))
    except PenPlotError as exc:
        return _error(exc.status, exc.code, exc.message)
    image_id = penplot_store.put_image_bytes(svg_text.encode("utf-8"), "svg")
    log.info(
        "wordmark.import text=%r mode=%s cuts=%d image=%s",
        body.text, body.mode, len(cuts_applied), image_id[:12],
    )
    return ImportResponse(
        image_id=image_id,
        text=body.text,
        font=body.font,
        mode=body.mode,
        cuts_applied=cuts_applied,  # type: ignore[arg-type]
        path_counts=path_counts,
        raw_counts=raw_counts,
        warnings=warnings,
    )


@router.get("/results/{token}", dependencies=[Depends(require_rate_limit)])
async def get_result(token: str) -> Response:
    """Serve a cached rendered SVG (sha1 token from ``svg_url``)."""
    if len(token) != 40 or any(c not in "0123456789abcdef" for c in token.lower()):
        return _error(404, "result_not_found", "Unknown wordmark result.")
    svg_text = await cache_get(f"{KEY_PREFIX}:svg:{token.lower()}")
    if svg_text is None:
        return _error(
            404, "result_not_found",
            "Wordmark result expired or unknown — re-run POST /v1/wordmark/render.",
        )
    return Response(content=svg_text, media_type="image/svg+xml")
