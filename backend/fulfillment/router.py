"""Fulfillment endpoints (fund-vista #147, shop #1079).

``POST /v1/fulfill/ink-split`` — one SVG per distinct ink from the purchased
design's layer-grouped draft. The request carries the design's HMAC token,
the render params persisted on the WordPress order (the shop half of #1079)
and the order's ink assignments.

The purchased design is read from the penplot image store first: the bytes
the buyer's import persisted (the order's ``POST /v1/images/{id}/retain``
keeps them for the retained TTL). The draft is re-rendered through the
cache-aware render path only as a fallback, when no intact stored copy
exists (missing, expired, not an SVG, not UTF-8, or hash drift). A
re-render never runs when the stored design is intact, so a renderer or
data-source change after purchase cannot break a paid order.

Guard rails:

- the token triple is verified with the shared shop secret (same HMAC as
  ``/v1/tokens``) — an unauthenticated fulfillment call never reads or renders;
- the draft used for the split — stored or re-rendered — must hash back to
  the ``design_id`` (sha256 of the exact bytes); the stored file name is
  never trusted. With no intact stored copy and a re-render that does not
  match, the call fails loudly with ``409 design_mismatch`` instead of
  plotting the wrong art;
- every pass file is validated by the same vector parser the import
  endpoints use, so an unplottable file can't leave the service.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
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
from backend.penplot.router import store as penplot_store
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


def _stored_design_svg(design_id: str) -> str | None:
    """The purchased design's own stored SVG, when it is intact.

    Returns the text only when the image store holds ``design_id`` as an
    ``.svg`` file, the bytes decode as UTF-8 and their digest equals
    ``design_id``. Anything else (missing, expired, non-SVG, not UTF-8, hash
    drift) returns None and the caller falls back to the re-render.
    """
    path = penplot_store.find_image(design_id)
    if path is None or os.path.splitext(path)[1].lower() != ".svg":
        return None
    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("fulfill.ink-split design=%s stored copy unreadable: %s",
                    design_id[:12], exc)
        return None
    if splitter.design_id_for(text) != design_id:
        log.warning("fulfill.ink-split design=%s stored copy fails the hash check",
                    design_id[:12])
        return None
    return text


_PLACE_RE = re.compile(r"^[A-Za-z0-9]{1,8}$")


def _place_from_params(params: dict[str, object]) -> str:
    """The file-title place for a stored design, without schema validation.

    The raw ``icao`` of the persisted params when it is a short alphanumeric
    string, else a generic label. Titles only; never used to render.
    """
    icao = params.get("icao")
    if isinstance(icao, str) and _PLACE_RE.match(icao.strip()):
        return icao.strip().upper()
    return "design"


@router.post("/ink-split", response_model=InkSplitResponse,
              dependencies=[Depends(require_rate_limit)])
async def ink_split(body: InkSplitRequest, request: Request) -> InkSplitResponse | JSONResponse:
    """Split the purchased design into one SVG per distinct ink.

    ``kind`` picks the artwork's renderer (airport or traffic); ``params`` is
    the exact render body persisted on the order. The purchased design is
    read from the image store first and hash-checked against ``design_id``.
    Only when no intact stored copy exists is the draft re-rendered (the
    cache-first render path), and that draft is hash-checked too. The
    draft is then split by the documented group-id → ink mapping; layers
    sharing an ink merge into one file, empty layers produce nothing.
    """
    started = time.monotonic()
    refused = _verify(body)
    if refused is not None:
        return refused
    if isinstance(body.params.get("kind"), str) and body.params["kind"] != body.kind:
        return error_response(422, "invalid_params",
                              "params.kind disagrees with the request kind.")

    endpoint = "/v1/fulfill/ink-split"
    design_id = body.design.design_id.lower()
    if body.kind == "traffic":
        attribution = TRAFFIC_ATTRIBUTION
        group_ink = _traffic_mapper(body.inks)
    else:
        attribution = AIRPORT_ATTRIBUTION
        group_ink = _airport_mapper(body.inks)

    # The stored design goes first and needs no schema check: an intact paid
    # design must not 422 because the persisted params drifted from the schema.
    stored = _stored_design_svg(design_id)
    if stored is not None:
        # The purchased bytes themselves: no renderer, no upstream calls.
        svg_text = stored
        place = _place_from_params(body.params)
        warnings, cache_hit = ["fulfilled_from_stored_design"], True
    else:
        parsed = _parse_params(body.kind, body.params)
        if isinstance(parsed, JSONResponse):
            return parsed
        if body.kind == "traffic":
            info = await traffic_router.render_document(parsed, request, endpoint, started)
            if isinstance(info, JSONResponse):
                return info
            svg_text, warnings, cache_hit = info["svg_text"], info["warnings"], info["cache_hit"]
            place = info["icao"]
        else:
            info = await airports_router.render_document(
                parsed, request, endpoint=endpoint, started_mono=started)
            if isinstance(info, JSONResponse):
                return info
            # design_id is the hash of the imported (content-cropped) image.
            svg_text = airports_router.crop_svg_to_content(info["svg_text"])
            warnings, cache_hit = info["warnings"], info["cache_hit"]
            place = info["icao"]

    # Whichever draft was used must be the purchased design: sha256 == design_id.
    digest = splitter.design_id_for(svg_text)
    if digest != design_id:
        log.warning("fulfill.ink-split design=%s mismatch: draft=%s",
                    design_id[:12], digest[:12])
        return error_response(
            409, "design_mismatch",
            "No intact stored copy of the purchased design exists and the re-rendered "
            "draft does not match it — the renderer or its data changed since purchase.",
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
