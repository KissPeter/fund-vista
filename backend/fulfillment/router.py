"""Fulfillment endpoints (fund-vista #147, shop #1079).

``POST /v1/fulfill/ink-split`` — one SVG per distinct ink from the purchased
design's layer-grouped draft. The request carries the design's HMAC token,
the render params persisted on the WordPress order (the shop half of #1079)
and the order's ink assignments; the draft is re-rendered through the same
cache-aware path the buyer's render used (the cache stays a pure
optimization — the WordPress order meta is the durable record).

Guard rails:

- the token triple is verified with the shared shop secret (same HMAC as
  ``/v1/tokens``) — an unauthenticated fulfillment call never renders;
- the re-rendered draft must hash back to the ``design_id`` (sha256 of the
  exact bytes) — a stale draft or a renderer-version bump fails loudly with
  ``409 design_mismatch`` instead of plotting the wrong art;
- every pass file is validated by the same vector parser the import
  endpoints use, so an unplottable file can't leave the service.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from backend.airports import router as airports_router
from backend.airports.schemas import ATTRIBUTION as AIRPORT_ATTRIBUTION
from backend.airports.schemas import RenderRequest as AirportRenderRequest
from backend.fulfillment import ink_split as splitter
from backend.fulfillment.schemas import (
    InkFileOut,
    InkSplitRequest,
    InkSplitResponse,
)
from backend.http import error_response
from backend.penplot import imaging
from backend.penplot.errors import PenPlotError
from backend.penplot import tokens as design_tokens
from backend.penplot.config import Settings
from backend.penplot.pipeline import run_convert
from backend.penplot.router import require_rate_limit
from backend.penplot.schemas import ConvertParams
from backend.penplot.svgmeta import stamp as svgmeta_stamp
from backend.traffic import router as traffic_router
from backend.traffic.schemas import ATTRIBUTION as TRAFFIC_ATTRIBUTION
from backend.traffic.schemas import RenderRequest as TrafficRenderRequest

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/fulfill", tags=["fulfillment-v1"])

_settings = Settings()


def _verify(body: InkSplitRequest) -> JSONResponse | None:
    """Enforce the design token: 410 expired, 403 otherwise-invalid."""
    valid, reason = design_tokens.verify_design_token(
        design_id=body.design.design_id.lower(),
        exp=body.design.exp,
        sig=body.design.sig,
        secret=_settings.hmac_secret,
        previous_secret=_settings.hmac_previous_secret,
    )
    if valid:
        return None
    log.warning("fulfill.ink-split design=%s refused: %s",
                body.design.design_id[:12], reason)
    if reason == "expired":
        return error_response(410, "token_expired",
                              "Design token expired; re-rendering needs a valid token.")
    return error_response(403, "token_invalid", f"Design token refused ({reason}).")


def _parse_params(kind: str, params: dict[str, object]) -> object | JSONResponse:
    """Validate the persisted render body against the kind's RenderRequest.

    The record carries its own ``kind`` marker key (the shop stores
    ``{kind, ...renderBody}``); it is not part of the render schema and must
    agree with the top-level ``kind``.
    """
    cleaned = {k: v for k, v in params.items() if k != "kind"}
    model = TrafficRenderRequest if kind == "traffic" else AirportRenderRequest
    try:
        return model(**cleaned)  # type: ignore[call-arg]
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()))
        return error_response(
            422, "invalid_params",
            f"Render params do not match the {kind} render schema"
            + (f" ({loc}: {first.get('msg')})" if loc else f": {exc.error_count()} error(s)."),
        )


@router.post("/ink-split", response_model=InkSplitResponse,
              dependencies=[Depends(require_rate_limit)])
async def ink_split(body: InkSplitRequest, request: Request) -> InkSplitResponse | JSONResponse:
    """Split the purchased design into one SVG per distinct ink.

    ``kind`` picks the draft source (the airport or the traffic renderer);
    ``params`` is the exact render body persisted on the order. The draft is
    fetched cache-first, re-rendered on a miss, verified against the
    ``design_id`` and split by the documented group-id → ink mapping;
    layers sharing an ink merge into one file, empty layers produce nothing.
    """
    started = time.monotonic()
    refused = _verify(body)
    if refused is not None:
        return refused
    if isinstance(body.params.get("kind"), str) and body.params["kind"] != body.kind:
        return error_response(422, "invalid_params",
                              "params.kind disagrees with the request kind.")

    endpoint = "/v1/fulfill/ink-split"
    if body.kind == "traffic":
        parsed = _parse_params(body.kind, body.params)
        if isinstance(parsed, JSONResponse):
            return parsed
        info = await traffic_router.render_document(parsed, request, endpoint, started)
        attribution = TRAFFIC_ATTRIBUTION
        if isinstance(info, JSONResponse):
            return info
        svg_text, warnings, cache_hit = info["svg_text"], info["warnings"], info["cache_hit"]
        place = info["icao"]
        group_ink = _traffic_mapper(body.inks)
    else:
        parsed = _parse_params(body.kind, body.params)
        if isinstance(parsed, JSONResponse):
            return parsed
        info = await airports_router.render_document(
            parsed, request, endpoint=endpoint, started_mono=started)
        attribution = AIRPORT_ATTRIBUTION
        if isinstance(info, JSONResponse):
            return info
        # design_id is the hash of the imported (content-cropped) image.
        svg_text = airports_router.crop_svg_to_content(info["svg_text"])
        warnings, cache_hit = info["warnings"], info["cache_hit"]
        place = info["icao"]
        group_ink = _airport_mapper(body.inks)

    # The re-rendered draft must be the purchased design: sha256 == design_id.
    digest = splitter.design_id_for(svg_text)
    if digest != body.design.design_id.lower():
        log.warning("fulfill.ink-split design=%s mismatch: re-render=%s",
                    body.design.design_id[:12], digest[:12])
        return error_response(
            409, "design_mismatch",
            "Re-rendered draft does not match the purchased design — the "
            "draft cache expired and the renderer changed since purchase.",
        )

    files: list[InkFileOut] = []
    passes = splitter.split_by_ink(svg_text, group_ink)
    # The page furniture (frame, title band, tables) is drawn once: on the Pen
    # pass, or on the first pass when the Pen has no layer of its own.
    furniture_ink = (
        body.inks.pen if any(p.ink == body.inks.pen for p in passes)
        else (passes[0].ink if passes else "")
    )
    for pass_file in passes:
        pass_svg = pass_file.svg
        if body.convert is not None:
            try:
                pass_svg = await asyncio.to_thread(
                    _compose_pass, pass_file, body.convert,
                    draw_furniture=pass_file.ink == furniture_ink, warnings=warnings,
                )
            except PenPlotError as exc:
                log.warning("fulfill.ink-split design=%s pass=%s compose failed: %s",
                            body.design.design_id[:12], pass_file.ink, exc.code)
                return error_response(exc.status, exc.code, exc.message)
        stamped = svgmeta_stamp(pass_svg, _settings, title=f"{place} — {pass_file.ink} pass")
        try:
            imaging.parse_svg_vectors(stamped.encode("utf-8"))
        except Exception as exc:  # unplottable pass: refuse the whole split
            log.warning("fulfill.ink-split design=%s pass=%s unplottable: %s",
                        body.design.design_id[:12], pass_file.ink, exc)
            return error_response(
                422, "unplottable_pass",
                f"The {pass_file.ink} pass is not plottable: {exc}",
            )
        files.append(InkFileOut(
            ink=pass_file.ink, hex=pass_file.hex,
            layers=pass_file.layers, svg=stamped,
        ))

    log.info(
        "fulfill.ink-split kind=%s design=%s place=%s files=%d cache_hit=%s %.2fs",
        body.kind, body.design.design_id[:12], place, len(files), cache_hit,
        time.monotonic() - started,
    )
    return InkSplitResponse(
        design_id=body.design.design_id.lower(),
        kind=body.kind, cache_hit=cache_hit,
        files=files, attribution=attribution, warnings=warnings,
    )


def _compose_pass(
    pass_file: splitter.InkFile,
    convert: ConvertParams,
    *,
    draw_furniture: bool,
    warnings: list[str],
) -> str:
    """Lay one ink pass out on the buyer's sheet (page, margins, title strip).

    The pass keeps the draft's root tag, so its source size — and with it the
    placement — is identical for every pass and for the preview convert. Only
    the Pen pass draws the furniture; the others reserve the same title strip
    so the artwork registers across pens. Every stroke is black on a plain page
    (the ink is carried by the file name, shop D0): the display colour and
    background of the preview never reach the file.
    """
    params = convert.model_copy(deep=True)
    params.background = "none"
    params.line_color = "black"
    data = pass_file.svg.encode("utf-8")
    result = run_convert(
        image_id=hashlib.sha256(data).hexdigest(),
        image_bytes=data, is_vector=True, src_w=0.0, src_h=0.0,
        params=params, settings=_settings, draw_furniture=draw_furniture,
    )
    for warning in result.warnings:
        if warning not in warnings:
            warnings.append(warning)
    return result.svg_text


def _airport_mapper(inks):
    """Group-id → ink mapping for the airport kind (runway groups → the
    runway ink, everything else → the Pen)."""
    def group_ink(group_id: str) -> str:
        return splitter.airport_group_ink(group_id, inks.pen, inks.runway)
    return group_ink


def _traffic_mapper(inks):
    """Group-id → ink mapping for the traffic kind (movement layers → their
    buyer ink, basemap/airfield/category/unknown → the Pen)."""
    def group_ink(group_id: str) -> str:
        return splitter.traffic_group_ink(group_id, inks.pen, inks.layer_colors)
    return group_ink
