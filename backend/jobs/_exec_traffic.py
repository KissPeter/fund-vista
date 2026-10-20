"""``traffic_ingest`` background job (plan §6.2, Phase 1).

Streams one adsb.lol day archive (~3.5 GB) and keeps the requesting
airport's slice as L1 in the durable slice store, then classifies into L2
movements in Redis. Runs on the NAS worker (never FastAPI Cloud —
512 MB / 60 s cannot hold the stream).

Cancel: checkpoints before/after the blocking pass plus a ``cancelled``
callback inside ``extract_multi`` (raises → mapped to cancelled by the
runner via :class:`ClientCancelled`). The L1 write is atomic (temp +
rename) and happens only after the whole stream succeeded, so a
cancelled/failed job never leaves a half dataset.

Singleflight note: same-IP double submits are already superseded by
``cancel_previous``; cross-worker same-day races are safe (L1 recheck
before download + atomic overwrite) but may download twice. A Redis NX
day-lock is the follow-up, not this phase.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import io
import json
import logging
import threading
from collections.abc import Callable

import httpx
from pydantic import ValidationError

from backend.airports.ourairports import (
    AirportNotFoundError,
    OurAirportsError,
    resolve_airport,
)
from backend.cancel import ClientCancelled
from backend.http import tls
from backend.jobs import store as _store
from backend.jobs._common import _JobError, _raise_if_cancelled
from backend.traffic import availability as _avail
from backend.traffic import cache as _cache
from backend.traffic import ingest as _ingest
from backend.traffic import store as _slices
from backend.traffic.hungary import MESSAGE as _HU_MESSAGE
from backend.traffic.hungary import is_hungarian_icao, row_is_hungarian
from backend.traffic.classify import CLASSIFIER_VERSION, classify_day
from backend.traffic.config import settings as _traffic_settings
from backend.traffic.schemas import ATTRIBUTION, TrafficIngestRequest, movements_key

log = logging.getLogger(__name__)

ENDPOINT = "/v1/jobs"


def _fnum(v: object) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _download_and_extract(
    day: dt.date,
    specs: list[dict],
    cancelled: Callable[[], bool],
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, list[dict]]:
    """Blocking archive pass (runs in a worker thread, sync httpx like the POC)."""
    timeout = httpx.Timeout(
        _traffic_settings.ingest_timeout_s,
        read=_traffic_settings.ingest_read_timeout_s,
    )
    with tls.client(timeout=timeout) as client:
        urls = _ingest.asset_urls(day, client)
        stream = io.BufferedReader(
            _ingest.ChunkReader(_ingest._chunks(urls, client)), 1 << 20
        )
        return _ingest.extract_multi(stream, specs, cancelled=cancelled)


async def _exec_traffic_ingest(
    payload: dict, job_id: str, stop: threading.Event, cancelled: Callable[[], bool]
) -> dict:
    try:
        body = TrafficIngestRequest(**payload)
    except ValidationError as exc:
        raise _JobError(422, "invalid_params", f"Invalid parameters: {exc.errors()[0]['msg']}" if exc.errors() else "Invalid parameters.")
    day = dt.date.fromisoformat(body.date)
    lo, hi = _avail.selectable_range()
    if not lo <= day <= hi:
        raise _JobError(422, "out_of_range", f"Date {body.date} outside {lo.isoformat()}..{hi.isoformat()}.")
    icao = body.icao.upper()
    if not is_hungarian_icao(icao):
        raise _JobError(422, "unsupported_airport", _HU_MESSAGE)

    store = _slices.default_store()
    key = _slices.day_key(day.isoformat(), icao)
    if store.exists(key):
        raw = store.get(key)
        if raw is not None:
            data = json.loads(gzip.decompress(raw).decode("utf-8"))
            moves = classify_day(data)
            await _store.set_progress(job_id, "cached", len(moves), len(moves))
            return {
                "icao": icao, "date": day.isoformat(),
                "movements": len(moves), "flights": len(data.get("flights", [])),
                "bytes": len(raw), "cache_hit": True, "attribution": ATTRIBUTION,
            }

    try:
        _slices.check_free_space()
    except OSError as exc:
        raise _JobError(507, "traffic_store_full", str(exc))

    _raise_if_cancelled(stop, ENDPOINT, "lookup")
    await _store.set_progress(job_id, "lookup")
    try:
        airport, _hit = await resolve_airport(icao)
    except AirportNotFoundError:
        raise _JobError(404, "airport_not_found", f"No airport found for ICAO '{icao}'.")
    except OurAirportsError as exc:
        raise _JobError(502, "ourairports_unavailable", f"Airport data fetch failed: {exc.detail}")
    if not row_is_hungarian(airport):
        raise _JobError(422, "unsupported_airport", _HU_MESSAGE)
    lat = _fnum(airport.get("latitude_deg"))
    lon = _fnum(airport.get("longitude_deg"))
    if lat is None or lon is None:
        raise _JobError(502, "ourairports_unavailable", f"Airport '{icao}' has no coordinates.")
    elev_ft = _fnum(airport.get("elevation_ft")) or 0.0
    bbox = _ingest.default_bbox(lat, lon)
    specs = [{"icao": icao, "lat": lat, "lon": lon, "elev_ft": elev_ft, "bbox": bbox}]

    _raise_if_cancelled(stop, ENDPOINT, "download")
    await _store.set_progress(job_id, "download")
    try:
        per_airport = await asyncio.to_thread(
            _download_and_extract, day, specs, stop.is_set
        )
    except RuntimeError as exc:
        if str(exc) == "cancelled" or stop.is_set():
            raise ClientCancelled(ENDPOINT, "download")
        raise
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise _JobError(502, "traffic_not_available", f"No archive release for {day.isoformat()}.")
        raise _JobError(502, "traffic_source_unavailable", f"Archive download failed: {exc}")
    except httpx.HTTPError as exc:
        raise _JobError(502, "traffic_source_unavailable", f"Archive download failed: {exc}")
    _raise_if_cancelled(stop, ENDPOINT, "classify")

    flights = per_airport.get(icao, [])
    data = {
        "icao": icao, "date": day.isoformat(),
        "airport": {"lat": lat, "lon": lon, "elev_ft": elev_ft},
        "bbox": list(bbox), "classifier": CLASSIFIER_VERSION,
        "source": "adsb.lol globe_history (ODbL)", "flights": flights,
    }
    await _store.set_progress(job_id, "store", len(flights), len(flights))
    raw = gzip.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"))
    await asyncio.to_thread(store.put, key, raw)

    try:
        marker_key = _slices.pass_key(day.isoformat())
        marker_raw = store.get(marker_key)
        marker = json.loads(marker_raw.decode("utf-8")) if marker_raw else {"date": day.isoformat(), "airports": []}
        if icao not in marker["airports"]:
            marker["airports"].append(icao)
        await asyncio.to_thread(store.put, marker_key, json.dumps(marker).encode("utf-8"))
    except Exception as exc:  # marker is bookkeeping — never fail the job for it
        log.warning("traffic pass-marker write failed day=%s: %r", day.isoformat(), exc)

    moves = classify_day(data)
    l2_key = _cache.traffic_cache_key("mov", movements_key(icao, day.isoformat()))
    try:
        await _cache.cache_set(l2_key, json.dumps(moves, separators=(",", ":")))
    except Exception as exc:
        log.warning("traffic L2 write failed %s: %r", l2_key, exc)

    log.info("traffic.ingest done icao=%s day=%s flights=%d moves=%d bytes=%d",
             icao, day.isoformat(), len(flights), len(moves), len(raw))
    return {
        "icao": icao, "date": day.isoformat(),
        "movements": len(moves), "flights": len(flights),
        "bytes": len(raw), "cache_hit": False, "attribution": ATTRIBUTION,
    }


__all__ = ["_exec_traffic_ingest"]
