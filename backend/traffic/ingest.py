"""adsb.lol ``globe_history`` daily archive -> compact per-airport JSON.

Streaming port of the ``tools/airport_traffic/ingest.py`` POC — the tar
walk keeps only aircraft that touched ground near the airport and never
stores the 3.5 GB archive. ``extract()`` is the single-airport POC shape
kept verbatim for tests; the ingest job uses ``extract_multi()`` so one
archive pass serves every airport queued for that day (plan §6.2).
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import multiprocessing as mp
import sys
import tarfile
from concurrent.futures import ProcessPoolExecutor
from typing import Iterator

import httpx

from backend.traffic.geo import km_between

REPO = "adsblol/globe_history_{year}"
TAG = "v{y}.{m:02d}.{d:02d}-planes-readsb-prod-0"
UA = "pen-pixel-shop-airport-traffic/0.1 (+https://github.com/KissPeter)"
NEAR_KM = 12.0
NEAR_AGL_FT = 4000


class ChunkReader(io.RawIOBase):
    """File-like over an iterator of byte chunks (lets tarfile stream across archive parts)."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._it, self._buf = chunks, b""

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:  # noqa: D102
        while not self._buf:
            try:
                self._buf = next(self._it)
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


def asset_urls(day: dt.date, client: httpx.Client) -> list[str]:
    tag = TAG.format(y=day.year, m=day.month, d=day.day)
    r = client.get("https://api.github.com/repos/%s/releases/tags/%s" % (REPO.format(year=day.year), tag),
                   headers={"User-Agent": UA})
    r.raise_for_status()
    assets = sorted(r.json()["assets"], key=lambda a: a["name"])
    return [a["browser_download_url"] for a in assets]


def _chunks(urls: list[str], client: httpx.Client) -> Iterator[bytes]:
    for url in urls:
        with client.stream("GET", url, follow_redirects=True, headers={"User-Agent": UA}) as r:
            r.raise_for_status()
            yield from r.iter_bytes(1 << 20)


def _near_points(trace: list, lat: float, lon: float, elev_ft: float) -> bool:
    for p in trace:
        if p[1] is None:
            continue
        a = p[3]
        if (a == "ground" or (a is not None and a - elev_ft < NEAR_AGL_FT)) and km_between(p[1], p[2], lat, lon) < NEAR_KM:
            return True
    return False


def _clip_flight(d: dict, base: float, bbox: tuple[float, float, float, float]) -> dict:
    lat0, lat1, lon0, lon1 = bbox
    tr = d.get("trace") or []
    pts, calls = [], []
    for p in tr:
        if p[1] is None:
            continue
        extra = p[8] if len(p) > 8 and isinstance(p[8], dict) else None
        if extra and extra.get("flight"):
            cs = extra["flight"].strip()
            if not calls or calls[-1][1] != cs:
                calls.append([round(base + p[0], 1), cs])
        if lat0 <= p[1] <= lat1 and lon0 <= p[2] <= lon1:
            gnd = p[3] == "ground"
            pts.append([round(base + p[0], 1), round(p[1], 5), round(p[2], 5), 0 if gnd else p[3], 1 if gnd else 0])
    return {"hex": d.get("icao"), "reg": d.get("r"), "type": d.get("t"), "desc": d.get("desc"),
            "db_flags": d.get("dbFlags") or 0, "operator": d.get("ownOp"), "calls": calls, "pts": pts}


def _iter_docs(stream) -> Iterator[dict]:
    tf = tarfile.open(fileobj=stream, mode="r|")
    for m in tf:
        if not m.isfile() or "trace_full_" not in m.name:
            continue
        raw = tf.extractfile(m).read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        try:
            yield json.loads(raw)
        except ValueError:
            continue


def _iter_member_payloads(stream) -> Iterator[bytes]:
    """Yield the raw (still gzipped) payload of every trace_full_* member.

    Same members ``_iter_docs`` reads, but without decompressing or
    parsing — the parallel path ships these bytes to worker processes.
    """
    tf = tarfile.open(fileobj=stream, mode="r|")
    for m in tf:
        if not m.isfile() or "trace_full_" not in m.name:
            continue
        yield tf.extractfile(m).read()


def _boxes(specs: list[dict]) -> list[tuple]:
    return [(s["icao"].upper(), s["lat"], s["lon"], s["elev_ft"], s["bbox"]) for s in specs]


def _absorb_doc(d: dict, boxes: list[tuple], out: dict[str, list[dict]]) -> None:
    """Filter one parsed aircraft doc into ``out`` (per-airport flights).

    One trace scan builds the doc's lat/lon bbox, then each airport whose
    bbox cannot contain the trace (rect disjoint from the trace bbox) is
    skipped without the O(trace) near-points scan. Exact, not heuristic:
    a point within ``NEAR_KM`` of an airport always lies inside that
    airport's ±span bbox (the airport is the bbox centre), so a rect miss
    implies ``_near_points`` false. Multi-airport passes therefore cost
    ~one single-airport pass plus a few rect tests per doc.
    """
    base, tr = d.get("timestamp", 0), d.get("trace") or []
    min_lat = min_lon = float("inf")
    max_lat = max_lon = float("-inf")
    for p in tr:
        lat, lon = p[1], p[2]
        if lat is None:
            continue
        if lat < min_lat:
            min_lat = lat
        if lat > max_lat:
            max_lat = lat
        if lon < min_lon:
            min_lon = lon
        if lon > max_lon:
            max_lon = lon
    if min_lat > max_lat or min_lon > max_lon:
        return  # no valid points at all
    for icao, lat, lon, elev_ft, bbox in boxes:
        lat0, lat1, lon0, lon1 = bbox
        if lat1 < min_lat or lat0 > max_lat or lon1 < min_lon or lon0 > max_lon:
            continue
        if _near_points(tr, lat, lon, elev_ft):
            out[icao].append(_clip_flight(d, base, bbox))


def _parse_batch(batch: list[bytes], boxes: list[tuple]) -> dict[str, list[dict]]:
    """Pool worker: gzipped member payloads -> flights per airport.

    Module level (picklable) and side-effect free: the parent merges the
    per-airport lists in archive order, so the result is identical to the
    serial pass.
    """
    out: dict[str, list[dict]] = {icao: [] for icao, *_ in boxes}
    for raw in batch:
        if raw[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        _absorb_doc(d, boxes, out)
    return out


def _merge_into(out: dict[str, list[dict]], part: dict[str, list[dict]]) -> None:
    for icao, flights in part.items():
        out[icao].extend(flights)


def extract(stream, lat: float, lon: float, elev_ft: float, bbox: tuple[float, float, float, float],
            progress: bool = False) -> list[dict]:
    """Walk a tar stream; keep traces that were on/near the ground at the airport."""
    out: list[dict] = []
    seen = 0
    for d in _iter_docs(stream):
        seen += 1
        if progress and seen % 5000 == 0:
            print("  seen %d aircraft, kept %d" % (seen, len(out)), file=sys.stderr, flush=True)
        base, tr = d.get("timestamp", 0), d.get("trace") or []
        if not _near_points(tr, lat, lon, elev_ft):
            continue
        out.append(_clip_flight(d, base, bbox))
    return out


def _extract_multi_serial(stream, specs: list[dict], progress: bool, cancelled) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {s["icao"].upper(): [] for s in specs}
    boxes = _boxes(specs)
    seen = 0
    for d in _iter_docs(stream):
        if cancelled is not None and cancelled():
            raise RuntimeError("cancelled")
        seen += 1
        if progress and seen % 5000 == 0:
            print("  seen %d aircraft" % seen, file=sys.stderr, flush=True)
        _absorb_doc(d, boxes, out)
    return out


def _extract_multi_parallel(stream, specs: list[dict], progress: bool, cancelled,
                            workers: int, batch_members: int) -> dict[str, list[dict]]:
    """Parse the archive in a process pool (the parse is the bottleneck).

    The parent streams the tar and ships raw member bytes to the workers
    (gzip + json + point scans); the download stays in the parent, so the
    network remains one sequential stream. Batches are merged back in
    archive order, so the result equals the serial pass byte for byte.
    """
    out: dict[str, list[dict]] = {s["icao"].upper(): [] for s in specs}
    boxes = _boxes(specs)
    ctx = mp.get_context("forkserver" if sys.platform.startswith("linux") else "spawn")
    seen = 0
    pending: dict[int, "Future"] = {}
    done: dict[int, dict] = {}
    next_idx = 0
    next_merge = 0
    batch: list[bytes] = []
    ex = ProcessPoolExecutor(max_workers=workers, mp_context=ctx)
    try:
        for raw in _iter_member_payloads(stream):
            if cancelled is not None and cancelled():
                raise RuntimeError("cancelled")
            batch.append(raw)
            if len(batch) < batch_members:
                continue
            pending[ex.submit(_parse_batch, batch, boxes)] = next_idx
            next_idx += 1
            batch = []
            seen += batch_members
            # Drain finished batches (results wait in `done` for ordering).
            for fut in list(pending):
                if fut.done():
                    done[pending.pop(fut)] = fut.result()
            # Merge in archive order as far as the results allow.
            while next_merge in done:
                _merge_into(out, done.pop(next_merge))
                next_merge += 1
            if progress and seen % 5000 == 0:
                print("  seen %d aircraft" % seen, file=sys.stderr, flush=True)
        if batch:
            pending[ex.submit(_parse_batch, batch, boxes)] = next_idx
            next_idx += 1
        for fut in list(pending):
            done[pending.pop(fut)] = fut.result()
        while next_merge in done:
            _merge_into(out, done.pop(next_merge))
            next_merge += 1
    finally:
        ex.shutdown(wait=True, cancel_futures=True)
    return out


def extract_multi(stream, specs: list[dict], progress: bool = False, cancelled=None,
                  workers: int = 0, batch_members: int = 200) -> dict[str, list[dict]]:
    """One archive pass for several airports (plan §6.2 multi-airport pass).

    ``specs`` entries: ``{icao, lat, lon, elev_ft, bbox}``. Returns
    ``{ICAO: [flights]}``. ``cancelled`` is an optional ``() -> bool``
    checkpoint (job cancel); raises ``ClientCancelled``-friendly
    ``RuntimeError("cancelled")`` so the job runner maps it to cancelled.

    ``workers`` > 1 parses the archive in a process pool (see
    ``_extract_multi_parallel``); ``workers`` <= 1 runs the original
    single-threaded pass. Both paths share ``_absorb_doc``, so they return
    the same flights in the same order.
    """
    if workers and workers > 1:
        return _extract_multi_parallel(stream, specs, progress, cancelled,
                                       workers, max(1, batch_members))
    return _extract_multi_serial(stream, specs, progress, cancelled)


def default_bbox(lat: float, lon: float, span_lat: float = 1.9, span_lon: float = 3.6) -> tuple[float, float, float, float]:
    return (lat - span_lat, lat + span_lat, lon - span_lon, lon + span_lon)
