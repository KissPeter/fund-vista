"""Convert background handler (REF-002 Phase 3).

Pure move from :mod:`backend.jobs.runner` — identical behavior, no
functional change.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

from backend.jobs import store as _store
from backend.jobs._common import _public_base, _raise_if_cancelled
from backend.penplot import imaging
from backend.penplot.errors import PenPlotError
from backend.penplot.pipeline import run_convert
from backend.penplot.router import (
    _settings,
    _warnings_for,
)
from backend.penplot.router import (
    store as penplot_store,
)
from backend.penplot.schemas import ConvertRequest


async def _exec_convert(
    payload: dict, job_id: str, stop: threading.Event, cancelled: Callable[[], bool]
) -> dict:
    req = ConvertRequest(**payload)
    endpoint = "/v1/convert"
    _raise_if_cancelled(stop, endpoint, "validation")
    image_id = req.image_id.lower()
    path = penplot_store.find_image(image_id)
    if path is None:
        raise PenPlotError(status=404, code="image_not_found",
                           message=f"Image '{image_id}' is not in cache. Re-upload via POST /v1/images.")
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else "png"
    is_vector = ext == "svg"
    _raise_if_cancelled(stop, endpoint, "decode")
    await _store.set_progress(job_id, "decode")
    with open(path, "rb") as fh:
        data = fh.read()
    if is_vector:
        _, vw, vh, _w = imaging.parse_svg_vectors(data)
        src_w, src_h = float(vw), float(vh)
    else:
        w, h, _fmt = imaging.probe_raster(data)
        src_w, src_h = float(w), float(h)
    _raise_if_cancelled(stop, endpoint, "vpype")
    await _store.set_progress(job_id, "vpype")
    result = await asyncio.to_thread(
        run_convert, image_id=image_id, image_bytes=data, is_vector=is_vector,
        src_w=src_w, src_h=src_h, params=req.params, settings=_settings,
        cancelled=cancelled,
    )
    _raise_if_cancelled(stop, endpoint, "vpype")
    penplot_store.put_result(result.filename, result.svg_text)
    base = _public_base()
    svg_url = f"{base}/v1/results/{result.filename}"
    warnings = list(result.warnings)
    if not is_vector and "low_resolution_for_a4" in _warnings_for(int(src_w), int(src_h), False):
        if "low_resolution_for_a4" not in warnings:
            warnings.append("low_resolution_for_a4")
    return {
        "image_id": image_id,
        "svg_url": svg_url,
        "vpype_command": result.vpype_command,
        "stats": result.stats.model_dump(mode="json"),
        "warnings": warnings,
    }


__all__ = ["_exec_convert"]
