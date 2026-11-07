"""``POST /v1/pcb/preview``: one Gerber/Excellon file in, one SVG out."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Response, UploadFile

from backend.pcb.render import MAX_UPLOAD_BYTES, render_preview_svg
from backend.penplot.router import require_rate_limit

router = APIRouter(prefix="/v1/pcb", tags=["pcb-v1"])


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
