"""Airport background handlers (REF-002 Phase 3).

Pure move from :mod:`backend.jobs.runner` — identical behavior, no
functional change. The render/import pair shares one private that takes
the endpoint string; each entry point only builds its own result dict.

NOTE (preserved defect, filed separately — not fixed in this refactor):
on an SVG-cache *miss* the shared path unpacks the 8-tuple returned by
:func:`backend.airports.router._render_uncached` into 4 names, which
raises ``ValueError`` and fails the job (hit path is unaffected). The
verbatim move below keeps that behavior; the fix belongs in its own
ticket with a miss-path regression test.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import NamedTuple

from fastapi.responses import JSONResponse

from backend.airports.router import (
    _bucket_radius_m,
    _effective_radius_m,
    _load_cached_render,
    _lookup,
    _render_keys,
    _render_uncached,
    _runway_infos,
    _store_render,
)
from backend.airports.schemas import RenderRequest
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


class _AirportDocument(NamedTuple):
    """Everything both airport entries need: art, context, and cache keys."""

    svg_text: str
    path_counts: dict[str, int]
    raw_counts: dict[str, int]
    rotation: float
    airport: dict
    runway_rows: list[dict]
    freq_rows: list[dict]
    icao: str
    warnings: list[str]
    cache_hit: bool
    svg_key: str


async def _render_document(
    body: RenderRequest, job_id: str, stop: threading.Event,
    cancelled: Callable[[], bool], endpoint: str,
) -> _AirportDocument:
    """Shared lookup → radius → keys → cached-or-build → store path.

    Raises :class:`_JobError` on error responses; ``ClientCancelled``
    propagates for the runner to map to cancelled. (On a cache miss the
    4-name unpack below raises — preserved defect, see module note.)
    """
    _ = cancelled  # stage loops live inside the router pipeline
    started = time.monotonic()
    req = _JobRequest(job_id, stop)
    resolved = await _lookup(body.icao)
    if isinstance(resolved, JSONResponse):
        raise _job_err(resolved)
    airport, runway_rows, freq_rows, warnings, _hit = resolved
    lat, lon = float(airport["latitude_deg"]), float(airport["longitude_deg"])
    icao = (airport.get("ident") or body.icao).strip().upper()
    radius_m = _bucket_radius_m(_effective_radius_m(airport, runway_rows, body.radius_m))
    if radius_m > body.radius_m:
        warnings.append("radius_expanded_to_cover_runways")
    svg_key, meta_key = _render_keys(icao, radius_m, body)
    cached = await _load_cached_render(svg_key, meta_key, warnings)
    if cached is not None:
        svg_text, path_counts, raw_counts, rotation = cached
        cache_hit = True
    else:
        cache_hit = False
        _raise_if_cancelled(stop, endpoint, "validation")
        await _store.set_progress(job_id, "overpass")
        built = await _render_uncached(
            airport, runway_rows, freq_rows, lat, lon, radius_m, body, warnings,
            request=req, endpoint=endpoint, started_mono=started, stop=stop,
        )
        if isinstance(built, JSONResponse):
            raise _job_err(built)
        svg_text, path_counts, raw_counts, rotation = built
        _raise_if_cancelled(stop, endpoint, "svg_build")
        await _store.set_progress(job_id, "svg_build")
        await _store_render(svg_key, meta_key, svg_text, path_counts, raw_counts, rotation)
    return _AirportDocument(
        svg_text, path_counts, raw_counts, rotation, airport, runway_rows,
        freq_rows, icao, warnings, cache_hit, svg_key,
    )


async def _exec_airport_render(
    payload: dict, job_id: str, stop: threading.Event, cancelled: Callable[[], bool]
) -> dict:
    body = RenderRequest(**payload)
    doc = await _render_document(body, job_id, stop, cancelled, "/v1/airports/render")
    base = _public_base()
    token = doc.svg_key.rsplit(":", 1)[-1]
    return {
        "icao": doc.icao,
        "name": doc.airport.get("name", ""),
        "municipality": doc.airport.get("municipality", ""),
        "iso_country": doc.airport.get("iso_country", ""),
        "runways": [
            {
                "le_ident": r.get("le_ident", ""),
                "he_ident": r.get("he_ident", ""),
                "length_ft": r.get("length_ft"),
                "width_ft": r.get("width_ft"),
                "surface": r.get("surface", ""),
                "le_heading_deg": r.get("le_heading_deg"),
                "he_heading_deg": r.get("he_heading_deg"),
                "endpoints_derived": False,
            }
            for r in _runway_infos(doc.runway_rows)
        ],
        "frequencies": [
            {"type": r.get("type", ""), "description": r.get("description", ""),
             "frequency_mhz": r.get("frequency_mhz")}
            for r in doc.freq_rows
        ],
        "rotation_deg": doc.rotation,
        "path_counts": doc.path_counts,
        "raw_counts": doc.raw_counts,
        "svg_url": f"{base}/v1/airports/results/{token}",
        "warnings": doc.warnings,
        "cache_hit": doc.cache_hit,
    }


async def _exec_airport_import(
    payload: dict, job_id: str, stop: threading.Event, cancelled: Callable[[], bool]
) -> dict:
    body = RenderRequest(**payload)
    doc = await _render_document(body, job_id, stop, cancelled, "/v1/airports/import")
    if sum(doc.path_counts.values()) == 0 and not doc.runway_rows:
        raise PenPlotError(status=422, code="invalid_params", message=(
            f"No diagram features found for '{doc.icao}' — unknown field or empty OSM coverage."))
    imaging.parse_svg_vectors(doc.svg_text.encode("utf-8"))
    image_id = penplot_store.put_image_bytes(doc.svg_text.encode("utf-8"), "svg")
    return {
        "image_id": image_id,
        "icao": doc.icao,
        "name": doc.airport.get("name", ""),
        "municipality": doc.airport.get("municipality", ""),
        "iso_country": doc.airport.get("iso_country", ""),
        "rotation_deg": doc.rotation,
        "path_counts": doc.path_counts,
        "raw_counts": doc.raw_counts,
        "warnings": doc.warnings,
    }


__all__ = ["_exec_airport_import", "_exec_airport_render"]
