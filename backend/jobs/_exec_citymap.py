"""Citymap background handlers (REF-002 Phase 3).

Pure move from :mod:`backend.jobs.runner` — identical behavior, no
functional change. The render/import pair shares one private that takes
the endpoint string; each entry point only builds its own result dict.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import NamedTuple

from fastapi.responses import JSONResponse

from backend.citymap.overpass import BBox, split_elements
from backend.citymap.render import render_svg
from backend.citymap.router import (
    _load_cached_render,
    _load_raw,
    _render_keys,
    _resolve_area,
    _store_render,
)
from backend.citymap.schemas import RenderRequest
from backend.jobs import store as _store
from backend.jobs._common import (
    _job_err,
    _JobRequest,
    _public_base,
    _raise_if_cancelled,
)
from backend.penplot import imaging
from backend.penplot.errors import PenPlotError
from backend.penplot.router import store as penplot_store


class _CitymapDocument(NamedTuple):
    """Everything both citymap entries need: art, context, and cache keys."""

    svg_text: str
    path_counts: dict[str, int]
    raw_counts: dict[str, int]
    city: str | None
    display_name: str | None
    bbox: BBox
    layers: list[str]
    warnings: list[str]
    cache_hit: bool
    svg_key: str


async def _render_document(
    body: RenderRequest, job_id: str, stop: threading.Event,
    cancelled: Callable[[], bool], endpoint: str,
) -> _CitymapDocument:
    """Shared resolve → keys → cached-or-fetch → build → store path.

    Raises :class:`_JobError` on error responses; ``ClientCancelled``
    propagates for the runner to map to cancelled.
    """
    started = time.monotonic()
    req = _JobRequest(job_id, stop)
    warnings: list[str] = []
    resolved = await _resolve_area(body, warnings)
    if isinstance(resolved, JSONResponse):
        raise _job_err(resolved)
    city, display_name, bbox = resolved
    layers = list(body.layers)
    svg_key, counts_key = _render_keys(bbox, layers, body)
    cached = await _load_cached_render(svg_key, counts_key, layers, warnings)
    if cached is not None:
        svg_text, path_counts, raw_counts = cached
        cache_hit = True
    else:
        cache_hit = False
        _raise_if_cancelled(stop, endpoint, "validation")
        await _store.set_progress(job_id, "overpass")
        raw_elements, err = await _load_raw(
            bbox, layers, warnings,
            request=req, endpoint=endpoint, started_mono=started,
        )
        if err is not None:
            raise _job_err(err)
        assert raw_elements is not None
        _raise_if_cancelled(stop, endpoint, "svg_build")
        await _store.set_progress(job_id, "svg_build")

        def _build():
            geoms, raw_counts_ = split_elements(
                raw_elements or [], layers, cancelled=cancelled)
            svg, path_counts_ = render_svg(
                geoms, bbox, layers, width=body.width,
                min_path_len_m=body.min_path_len_m, cancelled=cancelled,
            )
            return svg, path_counts_, raw_counts_

        svg_text, path_counts, raw_counts = await asyncio.to_thread(_build)
        _raise_if_cancelled(stop, endpoint, "svg_build")
        await _store_render(svg_key, counts_key, svg_text, path_counts, raw_counts)
    return _CitymapDocument(
        svg_text, path_counts, raw_counts, city, display_name, bbox,
        layers, warnings, cache_hit, svg_key,
    )


async def _exec_citymap_render(
    payload: dict, job_id: str, stop: threading.Event, cancelled: Callable[[], bool]
) -> dict:
    body = RenderRequest(**payload)
    doc = await _render_document(body, job_id, stop, cancelled, "/v1/citymap/render")
    base = _public_base()
    token = doc.svg_key.rsplit(":", 1)[-1]
    return {
        "city": doc.city,
        "display_name": doc.display_name,
        "bbox": {"south": doc.bbox[0], "west": doc.bbox[1], "north": doc.bbox[2], "east": doc.bbox[3]},
        "layers": doc.layers,
        "path_counts": doc.path_counts,
        "raw_counts": doc.raw_counts,
        "svg_url": f"{base}/v1/citymap/results/{token}",
        "attribution": "© OpenStreetMap contributors · ODbL 1.0 · https://osm.org/copyright",
        "warnings": doc.warnings,
        "cache_hit": doc.cache_hit,
    }


async def _exec_citymap_import(
    payload: dict, job_id: str, stop: threading.Event, cancelled: Callable[[], bool]
) -> dict:
    body = RenderRequest(**payload)
    doc = await _render_document(body, job_id, stop, cancelled, "/v1/citymap/import")
    if sum(doc.path_counts.values()) == 0:
        raise PenPlotError(status=422, code="invalid_params", message=(
            "No map features found for these layers in this area — "
            "tick more layers or use a bigger area."))
    imaging.parse_svg_vectors(doc.svg_text.encode("utf-8"))
    image_id = penplot_store.put_image_bytes(doc.svg_text.encode("utf-8"), "svg")
    return {
        "image_id": image_id,
        "city": doc.city,
        "display_name": doc.display_name,
        "bbox": {"south": doc.bbox[0], "west": doc.bbox[1], "north": doc.bbox[2], "east": doc.bbox[3]},
        "layers": doc.layers,
        "path_counts": doc.path_counts,
        "raw_counts": doc.raw_counts,
        "attribution": "© OpenStreetMap contributors · ODbL 1.0 · https://osm.org/copyright",
        "warnings": doc.warnings,
    }


__all__ = ["_exec_citymap_import", "_exec_citymap_render"]
