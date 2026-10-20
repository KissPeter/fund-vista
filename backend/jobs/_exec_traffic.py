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
from backend.traffic.hungary import row_is_hungarian
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
        return _ingest.extract_multi(
            stream, specs, cancelled=cancelled,
            workers=_traffic_settings.ingest_workers,
            batch_members=_traffic_settings.ingest_batch_members,
        )


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
    # Schema guarantees exactly one of the two shapes is set.
    multi = bool(body.icaos)
    icaos = list(body.icaos) if multi else [body.icao.upper()]
    single_icao = icaos[0]

    store = _slices.default_store()
    keys = {i: _slices.day_key(day.isoformat(), i) for i in icaos}

    # L1 recheck per airport — precedes lookup + download (contract), so
    # already-harvested airports cost nothing and are excluded from the
    # shared archive pass.
    cached: dict[str, dict] = {}
    for i in icaos:
        raw = store.get(keys[i]) if store.exists(keys[i]) else None
        if raw is not None:
            cached[i] = json.loads(gzip.decompress(raw).decode("utf-8"))

    if not multi:
        data = cached.get(single_icao)
        if data is not None:
            moves = classify_day(data)
            await _store.set_progress(job_id, "cached", len(moves), len(moves))
            return {
                "icao": single_icao, "date": day.isoformat(),
                "movements": len(moves), "flights": len(data.get("flights", [])),
                "bytes": len(gzip.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"))),
                "cache_hit": True, "attribution": ATTRIBUTION,
            }

    try:
        _slices.check_free_space()
    except OSError as exc:
        raise _JobError(507, "traffic_store_full", str(exc))

    _raise_if_cancelled(stop, ENDPOINT, "lookup")
    await _store.set_progress(job_id, "lookup")
    specs: list[dict] = []
    errors: list[dict] = []
    for i in (i for i in icaos if i not in cached):
        try:
            airport, _hit = await resolve_airport(i)
        except AirportNotFoundError:
            if not multi:
                raise _JobError(404, "airport_not_found", f"No airport found for ICAO '{i}'.")
            errors.append({"icao": i, "code": "airport_not_found",
                           "detail": f"No airport found for ICAO '{i}'."})
            continue
        except OurAirportsError as exc:
            if not multi:
                raise _JobError(502, "ourairports_unavailable", f"Airport data fetch failed: {exc.detail}")
            errors.append({"icao": i, "code": "ourairports_unavailable",
                           "detail": f"Airport data fetch failed: {exc.detail}"})
            continue
        if not row_is_hungarian(airport):
            if not multi:
                raise _JobError(422, "unsupported_airport", _HU_MESSAGE)
            errors.append({"icao": i, "code": "unsupported_airport", "detail": _HU_MESSAGE})
            continue
        lat = _fnum(airport.get("latitude_deg"))
        lon = _fnum(airport.get("longitude_deg"))
        if lat is None or lon is None:
            if not multi:
                raise _JobError(502, "ourairports_unavailable", f"Airport '{i}' has no coordinates.")
            errors.append({"icao": i, "code": "ourairports_unavailable",
                           "detail": f"Airport '{i}' has no coordinates."})
            continue
        elev_ft = _fnum(airport.get("elevation_ft")) or 0.0
        specs.append({"icao": i, "lat": lat, "lon": lon, "elev_ft": elev_ft,
                      "bbox": _ingest.default_bbox(lat, lon)})

    if specs:
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
    else:
        per_airport = {}
    _raise_if_cancelled(stop, ENDPOINT, "store")

    marker_key = _slices.pass_key(day.isoformat())
    marker_raw = store.get(marker_key)
    marker = json.loads(marker_raw.decode("utf-8")) if marker_raw else {"date": day.isoformat(), "airports": []}

    async def _store_slice(i: str, flights: list[dict]) -> tuple[int, int]:
        s = next(s for s in specs if s["icao"] == i)
        data = {
            "icao": i, "date": day.isoformat(),
            "airport": {"lat": s["lat"], "lon": s["lon"], "elev_ft": s["elev_ft"]},
            "bbox": list(s["bbox"]),
            "classifier": CLASSIFIER_VERSION,
            "source": "adsb.lol globe_history (ODbL)", "flights": flights,
        }
        raw = gzip.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"))
        await asyncio.to_thread(store.put, keys[i], raw)
        if i not in marker["airports"]:
            marker["airports"].append(i)
        moves = classify_day(data)
        l2_key = _cache.traffic_cache_key("mov", movements_key(i, day.isoformat()))
        try:
            await _cache.cache_set(l2_key, json.dumps(moves, separators=(",", ":")))
        except Exception as exc:  # L2 is a cache — never fail the job for it
            log.warning("traffic L2 write failed %s: %r", l2_key, exc)
        return len(raw), len(moves)

    entries: list[dict] = []
    spec_icaos = {s["icao"] for s in specs}
    for i in icaos:
        if i in cached:
            data = cached[i]
            flights = data.get("flights", [])
            moves = classify_day(data)
            if not multi:
                await _store.set_progress(job_id, "cached", len(moves), len(moves))
            entries.append({"icao": i, "movements": len(moves), "flights": len(flights),
                            "bytes": len(gzip.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"))),
                            "cache_hit": True})
            continue
        if i not in spec_icaos:
            continue  # resolution error — already in errors
        flights = per_airport.get(i, [])
        if not multi:
            await _store.set_progress(job_id, "store", len(flights), len(flights))
        raw_len, n_moves = await _store_slice(i, flights)
        entries.append({"icao": i, "movements": n_moves, "flights": len(flights),
                        "bytes": raw_len, "cache_hit": False})
        log.info("traffic.ingest done icao=%s day=%s flights=%d moves=%d bytes=%d",
                 i, day.isoformat(), len(flights), n_moves, raw_len)

    # pass marker is bookkeeping — never fail the job for it
    try:
        await asyncio.to_thread(store.put, marker_key, json.dumps(marker).encode("utf-8"))
    except Exception as exc:
        log.warning("traffic pass-marker write failed day=%s: %r", day.isoformat(), exc)

    if multi:
        return {"date": day.isoformat(), "airports": entries, "errors": errors,
                "attribution": ATTRIBUTION}

    entry = entries[0]
    return {
        "icao": single_icao, "date": day.isoformat(),
        "movements": entry["movements"], "flights": entry["flights"],
        "bytes": entry["bytes"], "cache_hit": entry["cache_hit"],
        "attribution": ATTRIBUTION,
    }


__all__ = ["_exec_traffic_ingest"]
