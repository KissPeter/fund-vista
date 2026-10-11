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
import sys
import tarfile
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


def extract_multi(stream, specs: list[dict], progress: bool = False, cancelled=None) -> dict[str, list[dict]]:
    """One archive pass for several airports (plan §6.2 multi-airport pass).

    ``specs`` entries: ``{icao, lat, lon, elev_ft, bbox}``. Returns
    ``{ICAO: [flights]}``. ``cancelled`` is an optional ``() -> bool``
    checkpoint (job cancel); raises ``ClientCancelled``-friendly
    ``RuntimeError("cancelled")`` so the job runner maps it to cancelled.
    """
    out: dict[str, list[dict]] = {s["icao"].upper(): [] for s in specs}
    seen = 0
    for d in _iter_docs(stream):
        if cancelled is not None and cancelled():
            raise RuntimeError("cancelled")
        seen += 1
        if progress and seen % 5000 == 0:
            print("  seen %d aircraft" % seen, file=sys.stderr, flush=True)
        base, tr = d.get("timestamp", 0), d.get("trace") or []
        for s in specs:
            if _near_points(tr, s["lat"], s["lon"], s["elev_ft"]):
                out[s["icao"].upper()].append(_clip_flight(d, base, s["bbox"]))
    return out


def default_bbox(lat: float, lon: float, span_lat: float = 1.9, span_lon: float = 3.6) -> tuple[float, float, float, float]:
    return (lat - span_lat, lat + span_lat, lon - span_lon, lon + span_lon)
