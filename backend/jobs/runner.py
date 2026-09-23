"""Background execution for /v1/jobs (plan P3.2).

Each worker keeps a local ``{job_id: asyncio.Task}`` map for tasks it runs;
Redis is the source of truth so GET/DELETE work from any of the 4 workers.
Cancel is polled every 200 ms (P2 ``POLL_S``): the runner watches both the
local ``threading.Event`` (set by a same-worker DELETE) and the Redis status
``cancelling`` (set by any-worker DELETE or ``cancel_previous`` supersede).
A ``queued`` job deleted before its task starts never runs.

Run path reuses the sync handler bodies refactored to take the parsed body +
a ``cancelled: () -> bool`` (P2 checkpoints poll it instead of the more
expensive ``is_disconnected()`` in background): Overpass races watch it,
vpype loops break on it, partial files are never stored. Max runtime 300 s
(matches nginx 310 s); on timeout the task is killed and the job fails with
``{error:{code:"timeout"}}``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from backend.cancel import ClientCancelled
from backend.jobs import store as _store
from backend.jobs.schemas import MAX_RUNTIME_S

log = logging.getLogger(__name__)

_tasks: dict[str, asyncio.Task] = {}
_stops: dict[str, threading.Event] = {}


class _JobRequest:
    """Minimal Request facade so P2 ``race_cancel`` works for background jobs.

    ``is_disconnected()`` is True once the job is cancelled (local event or
    Redis ``cancelling``/``cancelled``), letting Overpass fetches abort the
    same way an aborted sync POST does. ``headers`` carries no x-request-id;
    cancel logs use the job id as request_id via the endpoint label.
    """

    def __init__(self, job_id: str, stop: threading.Event) -> None:
        self._job_id = job_id
        self._stop = stop
        self.headers: dict[str, str] = {}

    async def is_disconnected(self) -> bool:  # noqa: D102
        if self._stop.is_set():
            return True
        try:
            doc = await _store.get_job(self._job_id)
        except Exception:
            return False
        return doc is not None and doc.get("status") in ("cancelling", "cancelled")


def enqueue(job_id: str) -> None:
    """Start ``run_job`` in this worker (no-op when already running)."""
    existing = _tasks.get(job_id)
    if existing is not None and not existing.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    stop = _stops.setdefault(job_id, threading.Event())
    # A job deleted while queued must never run: check before starting.
    task = loop.create_task(_run_job(job_id, stop))
    _tasks[job_id] = task

    def _done(_t: asyncio.Task) -> None:
        _tasks.pop(job_id, None)

    task.add_done_callback(_done)


def request_cancel_local(job_id: str) -> None:
    """Set the local stop event (same-worker DELETE fast path)."""
    stop = _stops.get(job_id)
    if stop is not None:
        stop.set()


def drop_queued(job_id: str) -> None:
    """If the task hasn't started running, cancel it so it never runs."""

    async def _drop() -> None:
        await asyncio.sleep(0)
        task = _tasks.get(job_id)
        doc = await _store.get_job(job_id)
        if (
            task is not None
            and not task.done()
            and doc is not None
            and doc.get("status") in ("cancelling", "cancelled")
            and doc.get("progress", {}).get("stage") == "queued"
        ):
            task.cancel()

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_drop())
    except RuntimeError:
        pass


def reset_local() -> None:
    """Test helper: forget local tasks/stops (running tasks are cancelled)."""
    for task in list(_tasks.values()):
        try:
            task.cancel()
        except Exception:
            pass
    _tasks.clear()
    _stops.clear()


async def _cancel_poller(job_id: str, stop: threading.Event) -> None:
    try:
        while not stop.is_set():
            try:
                doc = await _store.get_job(job_id)
            except Exception:
                doc = None
            if doc is not None and doc.get("status") in ("cancelling", "cancelled"):
                stop.set()
                return
            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        pass


async def _execute(
    job_type: str, payload: dict, job_id: str, stop: threading.Event
) -> dict:
    """Run the sync-equivalent body. Returns the ``result`` dict verbatim."""
    cancelled = stop.is_set
    if job_type == "citymap_render":
        return await _exec_citymap_render(payload, job_id, stop, cancelled)
    if job_type == "citymap_import":
        return await _exec_citymap_import(payload, job_id, stop, cancelled)
    if job_type == "airport_render":
        return await _exec_airport_render(payload, job_id, stop, cancelled)
    if job_type == "airport_import":
        return await _exec_airport_import(payload, job_id, stop, cancelled)
    if job_type == "convert":
        return await _exec_convert(payload, job_id, stop, cancelled)
    raise ValueError(f"Unknown job type '{job_type}'.")


def _raise_if_cancelled(stop: threading.Event, endpoint: str, stage: str) -> None:
    if stop.is_set():
        raise ClientCancelled(endpoint, stage)


async def _exec_citymap_render(
    payload: dict, job_id: str, stop: threading.Event, cancelled
) -> dict:
    import asyncio as _aio
    import threading as _th

    from backend.citymap.cache import citymap_cache_key
    from backend.citymap.overpass import bbox_str
    from backend.citymap.router import (
        _load_cached_render,
        _load_raw,
        _render_keys,
        _resolve_area,
    )
    from backend.citymap.schemas import RenderRequest
    from backend.penplot.router import resolve_public_base as _base

    body = RenderRequest(**payload)
    endpoint = "/v1/citymap/render"
    started = time.monotonic()
    req = _JobRequest(job_id, stop)
    warnings: list[str] = []
    resolved = await _resolve_area(body, warnings)
    from fastapi.responses import JSONResponse as _JR

    if isinstance(resolved, _JR):
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
            request=req, endpoint=endpoint, started_mono=started,  # type: ignore[arg-type]
        )
        if err is not None:
            raise _job_err(err)
        _raise_if_cancelled(stop, endpoint, "svg_build")
        await _store.set_progress(job_id, "svg_build")

        def _build():
            from backend.citymap.overpass import split_elements as _split
            from backend.citymap.render import render_svg as _render

            geoms, raw_counts_ = _split(raw_elements or [], layers, cancelled=cancelled)
            svg, path_counts_ = _render(
                geoms, bbox, layers, width=body.width,
                min_path_len_m=body.min_path_len_m, cancelled=cancelled,
            )
            return svg, path_counts_, raw_counts_

        try:
            svg_text, path_counts, raw_counts = await _aio.to_thread(_build)
        except ClientCancelled:
            raise
        _raise_if_cancelled(stop, endpoint, "svg_build")
        from backend.citymap.router import _store_render

        await _store_render(svg_key, counts_key, svg_text, path_counts, raw_counts)
    base = _public_base()
    token = svg_key.rsplit(":", 1)[-1]
    return {
        "city": city,
        "display_name": display_name,
        "bbox": {"south": bbox[0], "west": bbox[1], "north": bbox[2], "east": bbox[3]},
        "layers": layers,
        "path_counts": path_counts,
        "raw_counts": raw_counts,
        "svg_url": f"{base}/v1/citymap/results/{token}",
        "attribution": "© OpenStreetMap contributors · ODbL 1.0 · https://osm.org/copyright",
        "warnings": warnings,
        "cache_hit": cache_hit,
    }


async def _exec_citymap_import(
    payload: dict, job_id: str, stop: threading.Event, cancelled
) -> dict:
    import asyncio as _aio

    from backend.citymap.router import (
        _load_cached_render,
        _load_raw,
        _render_keys,
        _resolve_area,
    )
    from backend.citymap.schemas import RenderRequest

    body = RenderRequest(**payload)
    endpoint = "/v1/citymap/import"
    started = time.monotonic()
    req = _JobRequest(job_id, stop)
    warnings: list[str] = []
    resolved = await _resolve_area(body, warnings)
    from fastapi.responses import JSONResponse as _JR

    if isinstance(resolved, _JR):
        raise _job_err(resolved)
    city, display_name, bbox = resolved
    layers = list(body.layers)
    svg_key, counts_key = _render_keys(bbox, layers, body)
    cached = await _load_cached_render(svg_key, counts_key, layers, warnings)
    if cached is not None:
        svg_text, path_counts, raw_counts = cached
    else:
        _raise_if_cancelled(stop, endpoint, "validation")
        await _store.set_progress(job_id, "overpass")
        elements, err = await _load_raw(
            bbox, layers, warnings,
            request=req, endpoint=endpoint, started_mono=started,  # type: ignore[arg-type]
        )
        if err is not None:
            raise _job_err(err)
        assert elements is not None
        _raise_if_cancelled(stop, endpoint, "svg_build")
        await _store.set_progress(job_id, "svg_build")

        def _build():
            from backend.citymap.overpass import split_elements as _split
            from backend.citymap.render import render_svg as _render

            geoms, raw_counts_ = _split(elements or [], layers, cancelled=cancelled)
            svg, path_counts_ = _render(
                geoms, bbox, layers, width=body.width,
                min_path_len_m=body.min_path_len_m, cancelled=cancelled,
            )
            return svg, path_counts_, raw_counts_

        svg_text, path_counts, raw_counts = await _aio.to_thread(_build)
        _raise_if_cancelled(stop, endpoint, "svg_build")
        from backend.citymap.router import _store_render

        await _store_render(svg_key, counts_key, svg_text, path_counts, raw_counts)
    if sum(path_counts.values()) == 0:
        from backend.penplot.errors import PenPlotError as _PPE

        raise _PPE(status=422, code="invalid_params", message=(
            "No map features found for these layers in this area — "
            "tick more layers or use a bigger area."))
    from backend.penplot import imaging as _imaging
    from backend.penplot.router import store as _penplot_store

    _imaging.parse_svg_vectors(svg_text.encode("utf-8"))
    image_id = _penplot_store.put_image_bytes(svg_text.encode("utf-8"), "svg")
    return {
        "image_id": image_id,
        "city": city,
        "display_name": display_name,
        "bbox": {"south": bbox[0], "west": bbox[1], "north": bbox[2], "east": bbox[3]},
        "layers": layers,
        "path_counts": path_counts,
        "raw_counts": raw_counts,
        "attribution": "© OpenStreetMap contributors · ODbL 1.0 · https://osm.org/copyright",
        "warnings": warnings,
    }


async def _exec_airport_render(
    payload: dict, job_id: str, stop: threading.Event, cancelled
) -> dict:
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
    from fastapi.responses import JSONResponse as _JR

    body = RenderRequest(**payload)
    endpoint = "/v1/airports/render"
    started = time.monotonic()
    req = _JobRequest(job_id, stop)
    resolved = await _lookup(body.icao)
    if isinstance(resolved, _JR):
        raise _job_err(resolved)
    airport, runway_rows, freq_rows, warnings, _hit = resolved
    import math as _math

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
            request=req, endpoint=endpoint, started_mono=started, stop=stop,  # type: ignore[arg-type]
        )
        if isinstance(built, _JR):
            raise _job_err(built)
        svg_text, path_counts, raw_counts, rotation = built
        _raise_if_cancelled(stop, endpoint, "svg_build")
        await _store.set_progress(job_id, "svg_build")
        await _store_render(svg_key, meta_key, svg_text, path_counts, raw_counts, rotation)
    base = _public_base()
    token = svg_key.rsplit(":", 1)[-1]
    return {
        "icao": icao,
        "name": airport.get("name", ""),
        "municipality": airport.get("municipality", ""),
        "iso_country": airport.get("iso_country", ""),
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
            for r in _runway_infos(runway_rows)
        ],
        "frequencies": [
            {"type": r.get("type", ""), "description": r.get("description", ""),
             "frequency_mhz": r.get("frequency_mhz")}
            for r in freq_rows
        ],
        "rotation_deg": rotation,
        "path_counts": path_counts,
        "raw_counts": raw_counts,
        "svg_url": f"{base}/v1/airports/results/{token}",
        "warnings": warnings,
        "cache_hit": cache_hit,
    }


async def _exec_airport_import(
    payload: dict, job_id: str, stop: threading.Event, cancelled
) -> dict:
    from backend.airports.router import (
        _bucket_radius_m,
        _effective_radius_m,
        _load_cached_render,
        _lookup,
        _render_keys,
        _render_uncached,
        _store_render,
    )
    from backend.airports.schemas import RenderRequest
    from fastapi.responses import JSONResponse as _JR

    body = RenderRequest(**payload)
    endpoint = "/v1/airports/import"
    started = time.monotonic()
    req = _JobRequest(job_id, stop)
    resolved = await _lookup(body.icao)
    if isinstance(resolved, _JR):
        raise _job_err(resolved)
    airport, runway_rows, freq_rows, warnings, _ = resolved
    lat, lon = float(airport["latitude_deg"]), float(airport["longitude_deg"])
    icao = (airport.get("ident") or body.icao).strip().upper()
    radius_m = _bucket_radius_m(_effective_radius_m(airport, runway_rows, body.radius_m))
    if radius_m > body.radius_m:
        warnings.append("radius_expanded_to_cover_runways")
    svg_key, meta_key = _render_keys(icao, radius_m, body)
    cached = await _load_cached_render(svg_key, meta_key, warnings)
    if cached is not None:
        svg_text, path_counts, raw_counts, rotation = cached
    else:
        _raise_if_cancelled(stop, endpoint, "validation")
        await _store.set_progress(job_id, "overpass")
        built = await _render_uncached(
            airport, runway_rows, freq_rows, lat, lon, radius_m, body, warnings,
            request=req, endpoint=endpoint, started_mono=started, stop=stop,  # type: ignore[arg-type]
        )
        if isinstance(built, _JR):
            raise _job_err(built)
        svg_text, path_counts, raw_counts, rotation = built
        _raise_if_cancelled(stop, endpoint, "svg_build")
        await _store_render(svg_key, meta_key, svg_text, path_counts, raw_counts, rotation)
    if sum(path_counts.values()) == 0 and not runway_rows:
        from backend.penplot.errors import PenPlotError as _PPE

        raise _PPE(status=422, code="invalid_params", message=(
            f"No diagram features found for '{icao}' — unknown field or empty OSM coverage."))
    from backend.penplot import imaging as _imaging
    from backend.penplot.router import store as _penplot_store

    _imaging.parse_svg_vectors(svg_text.encode("utf-8"))
    image_id = _penplot_store.put_image_bytes(svg_text.encode("utf-8"), "svg")
    return {
        "image_id": image_id,
        "icao": icao,
        "name": airport.get("name", ""),
        "municipality": airport.get("municipality", ""),
        "iso_country": airport.get("iso_country", ""),
        "rotation_deg": rotation,
        "path_counts": path_counts,
        "raw_counts": raw_counts,
        "warnings": warnings,
    }


async def _exec_convert(
    payload: dict, job_id: str, stop: threading.Event, cancelled
) -> dict:
    import asyncio as _aio

    from backend.penplot import imaging as _imaging
    from backend.penplot.pipeline import run_convert
    from backend.penplot.router import (
        _settings as _settings,
        _warnings_for,
        resolve_public_base as _resolve_base,
        store as _penplot_store,
    )
    from backend.penplot.schemas import ConvertRequest

    req = ConvertRequest(**payload)
    endpoint = "/v1/convert"
    _raise_if_cancelled(stop, endpoint, "validation")
    image_id = req.image_id.lower()
    path = _penplot_store.find_image(image_id)
    if path is None:
        from backend.penplot.errors import PenPlotError as _PPE

        raise _PPE(status=404, code="image_not_found",
                   message=f"Image '{image_id}' is not in cache. Re-upload via POST /v1/images.")
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else "png"
    is_vector = ext == "svg"
    _raise_if_cancelled(stop, endpoint, "decode")
    await _store.set_progress(job_id, "decode")
    with open(path, "rb") as fh:
        data = fh.read()
    if is_vector:
        _, vw, vh, _w = _imaging.parse_svg_vectors(data)
        src_w, src_h = float(vw), float(vh)
    else:
        w, h, _fmt = _imaging.probe_raster(data)
        src_w, src_h = float(w), float(h)
    _raise_if_cancelled(stop, endpoint, "vpype")
    await _store.set_progress(job_id, "vpype")
    result = await _aio.to_thread(
        run_convert, image_id=image_id, image_bytes=data, is_vector=is_vector,
        src_w=src_w, src_h=src_h, params=req.params, settings=_settings,
        cancelled=cancelled,
    )
    _raise_if_cancelled(stop, endpoint, "vpype")
    _penplot_store.put_result(result.filename, result.svg_text)
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


def _public_base() -> str:
    try:
        from backend.penplot.router import _settings as _s

        override = _s.public_base_url.strip().rstrip("/")
        if override:
            return override
    except Exception:
        pass
    return "http://localhost"


class _JobError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _job_err(resp) -> _JobError:
    try:
        body = resp.body.decode() if hasattr(resp, "body") else "{}"
        import json as _json

        data = _json.loads(body)
        err = data.get("error", {})
        return _JobError(resp.status_code, err.get("code", "processing_failed"),
                         err.get("message", "Job failed."))
    except Exception:
        return _JobError(500, "processing_failed", "Job failed.")


# Map job-failure exceptions to stored ``error`` envelopes (sync tokens kept).
async def _store_job_exception(job_id: str, exc: BaseException) -> None:
    from backend.penplot.errors import PenPlotError as _PPE

    if isinstance(exc, _JobError):
        await _store.set_error(job_id, exc.code, exc.message)
    elif isinstance(exc, _PPE):
        await _store.set_error(job_id, exc.code, exc.message)
    else:
        await _store.set_error(job_id, "processing_failed", "Image processing failed.")


# Main entry: maps _JobError/PenPlotError → failed envelope (sync tokens kept).
async def _run_job(job_id: str, stop: threading.Event) -> None:
    doc = await _store.get_job(job_id)
    if doc is None:
        return
    if doc.get("status") in ("cancelling", "cancelled"):
        await _store.set_cancelled(job_id)
        _stops.pop(job_id, None)
        return
    job_type = doc["type"]
    payload = doc.get("request", {})
    started = time.monotonic()
    await _store.set_status(job_id, "running", progress={"stage": "running", "done": 0, "total": 0})
    poller = asyncio.create_task(_cancel_poller(job_id, stop))
    try:
        try:
            result = await asyncio.wait_for(
                _execute(job_type, payload, job_id, stop),
                timeout=MAX_RUNTIME_S,
            )
        except asyncio.TimeoutError:
            log.warning("jobs.timeout id=%s type=%s", job_id[:12], job_type)
            await _store.set_error(job_id, "timeout", "Job exceeded 300 s and was stopped.")
            return
        except asyncio.CancelledError:
            await _store.set_cancelled(job_id)
            return
        except ClientCancelled:
            await _store.set_cancelled(job_id)
            log.info("jobs.cancel id=%s type=%s", job_id[:12], job_type)
            return
        except Exception as exc:  # noqa: BLE001 — mapped to failed envelope
            if isinstance(exc, asyncio.CancelledError):
                await _store.set_cancelled(job_id)
                return
            await _store_job_exception(job_id, exc)
            log.warning("jobs.failed id=%s type=%s: %r", job_id[:12], job_type, exc)
            return
        cur = await _store.get_job(job_id)
        if cur is not None and cur.get("status") in ("cancelling", "cancelled"):
            await _store.set_cancelled(job_id)
            return
        await _store.set_result(job_id, result)
        log.info(
            "jobs.done id=%s type=%s elapsed_ms=%d",
            job_id[:12], job_type, int((time.monotonic() - started) * 1000),
        )
    finally:
        poller.cancel()
        _stops.pop(job_id, None)


__all__ = [
    "enqueue",
    "request_cancel_local",
    "drop_queued",
    "reset_local",
]
