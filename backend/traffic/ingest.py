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
import queue
import sys
import tarfile
import threading
from concurrent.futures import ProcessPoolExecutor
from typing import Callable, Iterator

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


def asset_assets(day: dt.date, client: httpx.Client) -> list[dict]:
    """The day archive's release assets: name, download url and size (bytes).

    The size comes from the release metadata, so the job can show a real
    progress total without a second HEAD request per part.
    """
    tag = TAG.format(y=day.year, m=day.month, d=day.day)
    r = client.get("https://api.github.com/repos/%s/releases/tags/%s" % (REPO.format(year=day.year), tag),
                   headers={"User-Agent": UA})
    r.raise_for_status()
    assets = sorted(r.json()["assets"], key=lambda a: a["name"])
    return [{"name": a["name"], "url": a["browser_download_url"],
             "size": int(a.get("size") or 0)} for a in assets]


def asset_urls(day: dt.date, client: httpx.Client) -> list[str]:
    return [a["url"] for a in asset_assets(day, client)]


def _chunks(urls: list[str], client: httpx.Client,
            on_progress: Callable[[dict], None] | None = None,
            cancelled: Callable[[], bool] | None = None,
            total: int = 0) -> Iterator[bytes]:
    """Serial download: the parts are fetched one after the other."""
    seen = 0
    for url in urls:
        if cancelled is not None and cancelled():
            raise RuntimeError("cancelled")
        with client.stream("GET", url, follow_redirects=True, headers={"User-Agent": UA}) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(1 << 20):
                seen += len(chunk)
                if on_progress is not None:
                    on_progress({"stage": "download", "done": seen, "total": total})
                yield chunk


def _put(q: "queue.Queue", item, stop: threading.Event) -> None:
    """Queue put that never blocks forever (the consumer may abort)."""
    while not stop.is_set():
        try:
            q.put(item, timeout=0.2)
            return
        except queue.Full:
            continue


def _parallel_chunks(assets: list[dict], client: httpx.Client, workers: int,
                     queue_size: int = 4,
                     on_progress: Callable[[dict], None] | None = None,
                     cancelled: Callable[[], bool] | None = None) -> Iterator[bytes]:
    """Download the archive parts concurrently, yield their bytes in order.

    The parts are the segments of one split tar, so the consumer needs them
    in order — but they can be *fetched* in parallel: every part has its own
    bounded queue, one thread per part fills it, and the consumer drains the
    queues in archive order. Memory stays bounded by
    ``parts * queue_size * chunk_size``; the network, which was the last
    serial stage of the pass, now overlaps with the parse.

    Errors are not swallowed: the first failing part raises its exception
    (``httpx.HTTPStatusError`` and friends) in the consumer thread.
    """
    if not assets:
        return
    stop = threading.Event()
    total = sum(int(a.get("size") or 0) for a in assets)
    counter = {"bytes": 0}
    counter_lock = threading.Lock()

    def _notify(n: int) -> None:
        if on_progress is None:
            return
        with counter_lock:
            counter["bytes"] += n
            done = counter["bytes"]
        on_progress({"stage": "download", "done": done, "total": total})

    def _fetch(asset: dict, q: "queue.Queue") -> None:
        try:
            with client.stream("GET", asset["url"], follow_redirects=True,
                               headers={"User-Agent": UA}) as r:
                r.raise_for_status()
                for chunk in r.iter_bytes(1 << 20):
                    if stop.is_set():
                        return
                    _put(q, chunk, stop)
                    if stop.is_set():
                        return
                    _notify(len(chunk))
        except BaseException as exc:  # noqa: BLE001 - forwarded to the consumer
            _put(q, exc, stop)
        finally:
            _put(q, None, stop)

    queues: list["queue.Queue"] = [queue.Queue(maxsize=max(1, queue_size)) for _ in assets]
    threads: list[threading.Thread] = []
    for asset, q in zip(assets, queues):
        t = threading.Thread(target=_fetch, args=(asset, q), daemon=True)
        t.start()
        threads.append(t)
    try:
        for q in queues:
            while True:
                if cancelled is not None and cancelled():
                    raise RuntimeError("cancelled")
                try:
                    # A short timeout keeps the cancel check responsive even
                    # when a part stalls.
                    item = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)


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


def _extract_multi_serial(stream, specs: list[dict], progress: bool, cancelled,
                          on_progress: Callable[[dict], None] | None = None) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {s["icao"].upper(): [] for s in specs}
    boxes = _boxes(specs)
    seen = 0
    for d in _iter_docs(stream):
        if cancelled is not None and cancelled():
            raise RuntimeError("cancelled")
        seen += 1
        if progress and seen % 5000 == 0:
            print("  seen %d aircraft" % seen, file=sys.stderr, flush=True)
        if on_progress is not None and seen % 500 == 0:
            on_progress({"stage": "parse", "done": seen, "total": 0})
        _absorb_doc(d, boxes, out)
    if on_progress is not None:
        on_progress({"stage": "parse", "done": seen, "total": 0})
    return out


def _extract_multi_parallel(stream, specs: list[dict], progress: bool, cancelled,
                            workers: int, batch_members: int,
                            on_progress: Callable[[dict], None] | None = None) -> dict[str, list[dict]]:
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
            seen += 1
            if len(batch) < batch_members:
                continue
            pending[ex.submit(_parse_batch, batch, boxes)] = next_idx
            next_idx += 1
            batch = []
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
            if on_progress is not None and seen % 500 == 0:
                on_progress({"stage": "parse", "done": seen, "total": 0})
        if batch:
            pending[ex.submit(_parse_batch, batch, boxes)] = next_idx
            next_idx += 1
        for fut in list(pending):
            done[pending.pop(fut)] = fut.result()
        while next_merge in done:
            _merge_into(out, done.pop(next_merge))
            next_merge += 1
        if on_progress is not None:
            on_progress({"stage": "parse", "done": seen, "total": 0})
    finally:
        ex.shutdown(wait=True, cancel_futures=True)
    return out


def extract_multi(stream, specs: list[dict], progress: bool = False, cancelled=None,
                  workers: int = 0, batch_members: int = 200,
                  on_progress: Callable[[dict], None] | None = None) -> dict[str, list[dict]]:
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
                                       workers, max(1, batch_members), on_progress)
    return _extract_multi_serial(stream, specs, progress, cancelled, on_progress)


def default_bbox(lat: float, lon: float, span_lat: float = 1.9, span_lon: float = 3.6) -> tuple[float, float, float, float]:
    return (lat - span_lat, lat + span_lat, lon - span_lon, lon + span_lon)
