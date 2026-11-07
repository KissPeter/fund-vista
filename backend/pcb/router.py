"""``POST /v1/pcb/preview``: one Gerber/Excellon file in, one SVG out.

``POST /v1/pcb/render``: one file plus its render params in, one A4 sheet out.
Only the sheet's enabled sub-layers are plotted, each stroked in its ink.
The SVG has no ``<image>`` and no ``<text>``.

The shop is built against the request and response below.

Request (multipart/form-data)::

    file:   board.gbr                     (Gerber or Excellon, at most 5 MB)
    params: {"paper": "white", "pen": "black",
             "layers": {"pads": {"enabled": true, "ink": "auto"},
                        "tracks": {"enabled": true, "ink": "blue"},
                        "pours": {"enabled": false, "ink": "auto"},
                        "drills": {"enabled": false, "ink": "auto"}},
             "mirror": false}

Response 200 (application/json, abridged; svg shortened)::

    {
      "svg": "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"210mm\" ...>...</svg>\n",
      "svg_url": "https://api.example/v1/pcb/results/<design_id>",
      "stats": {"strokes": 5, "pen_down_mm": 587.67,
                "pen_up_mm": 621.11, "estimated_time_s": 23.8},
      "layers": [{"id": "pads", "ink": "black", "paths": 3},
                 {"id": "tracks", "ink": "blue", "paths": 2}],
      "paper": "white", "pen": "black", "mirror": false, "rotated": true,
      "design": {"design_id": "<sha256 of svg>", "exp": 1767000000,
                 "sig": "<hex HMAC-SHA256>"},
      "warnings": [], "cache_hit": false
    }

``GET /v1/pcb/results/{design_id}/meta`` returns the stats and the validated
render params that produced the cached sheet, so the shop resolves price,
paper, pen and inks server-side::

    GET /v1/pcb/results/<design_id>/meta
    200 {"stats": {"strokes": 5, "pen_down_mm": 587.67,
                   "pen_up_mm": 621.11, "estimated_time_s": 23.8},
         "params": {"paper": "white", "pen": "black", "mirror": false,
                    "layers": {"pads": {"enabled": true, "ink": "auto"},
                               "tracks": {"enabled": true, "ink": "blue"},
                               "pours": {"enabled": false, "ink": "auto"},
                               "drills": {"enabled": false, "ink": "auto"}}}}
    404 {"error": {"code": "result_not_found", ...}}  unknown id or past the 24 h window

Errors use the usual envelope ``{"error": {"code", "message"}}``: 413 above
5 MB, 422 ``invalid_params`` for a bad file, bad params (for example white
ink on white paper), no enabled sub-layer, or a board over the plot ceiling
(20000 mm pen-down or 1800 s estimated time).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from backend.http import error_response
from backend.pcb import cache as pcb_cache
from backend.pcb.config import settings as pcb_settings
from backend.pcb.layers import board_shapes
from backend.pcb.render import MAX_UPLOAD_BYTES, render_preview_svg, run_isolated
from backend.pcb.schemas import (
    DesignToken,
    PcbMetaResponse,
    PcbRenderParams,
    PcbRenderResponse,
    SheetStats,
)
from backend.pcb.sheet import compose_sheet
from backend.fulfillment.ink_split import design_id_for
from backend.penplot import tokens as design_tokens
from backend.penplot.config import Settings
from backend.penplot.errors import ErrorCode, PenPlotError
from backend.penplot.router import require_rate_limit, resolve_public_base

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/pcb", tags=["pcb-v1"])

_settings = Settings()

#: Bump when the sheet geometry or the stats change, so stale renders miss.
SHEET_VERSION = "sheet-v1"


@router.post(
    "/preview",
    response_class=Response,
    responses={200: {"content": {"image/svg+xml": {}}}},
    dependencies=[Depends(require_rate_limit)],
)
async def preview(file: UploadFile = File(...)) -> Response:
    # Read one byte past the cap so an oversize upload is detected without
    # buffering the whole body.
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    svg = await render_preview_svg(data)
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"cache-control": "no-store"},
    )


def _sign(design_id: str) -> tuple[DesignToken | None, list[str]]:
    """The design token triple, or None with a warning when unsigned."""
    if not _settings.hmac_secret:
        return None, ["token_signing_unavailable"]
    exp = int(time.time()) + _settings.token_ttl_hours * 3600
    sig = design_tokens.sign_design_token(
        design_id=design_id, exp=exp, secret=_settings.hmac_secret
    )
    return DesignToken(design_id=design_id, exp=exp, sig=sig), []


async def _load_cached(request_key: str) -> dict | None:
    """The cached render for this request, or None when any part is missing."""
    found = await pcb_cache.cache_get(request_key)
    if found is None:
        return None
    try:
        meta = json.loads(found)
    except ValueError:
        return None
    design_id = meta["design_id"]
    parts = await pcb_cache.cache_get_many([
        pcb_cache.svg_cache_key(design_id),
        pcb_cache.params_cache_key(design_id),
        pcb_cache.meta_cache_key(design_id),
    ])
    if len(parts) != 3:
        return None
    meta["svg"] = parts[pcb_cache.svg_cache_key(design_id)]
    return meta


@router.post("/render", response_model=PcbRenderResponse,
             dependencies=[Depends(require_rate_limit)])
async def render(
    request: Request,
    file: UploadFile = File(...),
    params: str = Form(...),
) -> PcbRenderResponse | JSONResponse:
    """Plot one Gerber or Excellon file on one A4 sheet (see module docstring)."""
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise PenPlotError(
            status=413, code=ErrorCode.PAYLOAD_TOO_LARGE,
            message="The Gerber file is larger than 5 MB.",
        )
    try:
        spec = PcbRenderParams.model_validate_json(params)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        return error_response(
            422, ErrorCode.INVALID_PARAMS,
            f"Render params refused: {first.get('msg', 'invalid params')}",
        )

    canonical = spec.model_dump_json()
    request_key = pcb_cache.pcb_cache_key(
        "render", SHEET_VERSION, hashlib.sha256(data).hexdigest(), canonical,
    )
    cached = await _load_cached(request_key)
    if cached is not None:
        design_id, svg = cached["design_id"], cached["svg"]
        token, token_warnings = _sign(design_id)
        return _response(request, spec, svg, design_id, token, cached, token_warnings, True)

    shapes = await asyncio.to_thread(run_isolated, "backend.pcb.layers:board_shapes", data)
    result = compose_sheet(shapes, spec)
    svg = result.svg
    design_id = design_id_for(svg)
    token, token_warnings = _sign(design_id)
    meta = {
        "design_id": design_id,
        "stats": result.stats.model_dump(),
        "layers": [layer.model_dump() for layer in result.layers],
        "rotated": result.rotated,
        "warnings": result.warnings,
    }
    await pcb_cache.cache_set_many({
        pcb_cache.svg_cache_key(design_id): svg,
        pcb_cache.params_cache_key(design_id): canonical,
        pcb_cache.meta_cache_key(design_id): json.dumps(meta["stats"]),
        request_key: json.dumps(meta),
    }, ttl_s=pcb_settings.cache_ttl_hours * 3600)
    log.info(
        "pcb.render layers=%s strokes=%d pen_down=%.1fmm design=%s",
        ",".join(layer.id for layer in result.layers), result.stats.strokes,
        result.stats.pen_down_mm, design_id[:12],
    )
    return _response(request, spec, svg, design_id, token, meta, token_warnings, False)


def _response(
    request: Request,
    spec: PcbRenderParams,
    svg: str,
    design_id: str,
    token: DesignToken | None,
    meta: dict,
    token_warnings: list[str],
    cache_hit: bool,
) -> PcbRenderResponse:
    base = resolve_public_base(request)
    return PcbRenderResponse(
        svg=svg,
        svg_url=f"{base}/v1/pcb/results/{design_id}",
        stats=meta["stats"],
        layers=meta["layers"],
        paper=spec.paper,
        pen=spec.pen,
        mirror=spec.mirror,
        rotated=meta["rotated"],
        design=token,
        warnings=[*meta["warnings"], *token_warnings],
        cache_hit=cache_hit,
    )


@router.get("/results/{design_id}/meta", response_model=PcbMetaResponse,
            dependencies=[Depends(require_rate_limit)])
async def get_meta(design_id: str) -> PcbMetaResponse | JSONResponse:
    """The stats and the validated render params of a cached sheet.

    The shop resolves price, paper, pen and inks from this, never from the
    browser. 404 once the cache window (24 h) has passed.
    """
    token = design_id.lower()
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        return error_response(404, "result_not_found", "Unknown PCB result.")
    found = await pcb_cache.cache_get_many([
        pcb_cache.svg_cache_key(token),
        pcb_cache.params_cache_key(token),
        pcb_cache.meta_cache_key(token),
    ])
    if len(found) != 3:
        return error_response(
            404, "result_not_found",
            "PCB result expired or unknown - re-run POST /v1/pcb/render.",
        )
    return PcbMetaResponse(
        stats=SheetStats(**json.loads(found[pcb_cache.meta_cache_key(token)])),
        params=PcbRenderParams.model_validate_json(found[pcb_cache.params_cache_key(token)]),
    )


@router.get("/results/{design_id}", dependencies=[Depends(require_rate_limit)])
async def get_result(design_id: str) -> Response:
    """Serve a cached rendered sheet SVG (the ``design_id`` from ``svg_url``)."""
    token = design_id.lower()
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        return error_response(404, "result_not_found", "Unknown PCB result.")
    svg = await pcb_cache.cache_get(pcb_cache.svg_cache_key(token))
    if svg is None:
        return error_response(
            404, "result_not_found",
            "PCB result expired or unknown - re-run POST /v1/pcb/render.",
        )
    return Response(content=svg, media_type="image/svg+xml")

