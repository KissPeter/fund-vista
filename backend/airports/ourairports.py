"""OurAirports (CC0) data access: airport/runway/frequency CSVs.

CSVs are fetched from the upstream GitHub repo on first use and cached
(Redis-first via :mod:`backend.airports.cache`, ``ourairports:<name>`` keys)
for ``cache_ttl_hours``. All parsing is stdlib ``csv`` — no new deps.

Runway endpoint fallback (spec §1): ``le_/he_latitude_deg`` are often empty
for small fields — when missing, endpoints are derived from the airport
center ± half the runway length along the ident heading (``"13L" → 130°``).
"""

from __future__ import annotations

import csv
import io
import logging
import re

import httpx

from backend.airports import cache as cache_mod
from backend.airports.config import settings

log = logging.getLogger(__name__)

FT_TO_M = 0.3048
_M_PER_DEG_LAT = 110540.0
_M_PER_DEG_LON_EQUATOR = 111320.0

_IDENT_HEADING_RE = re.compile(r"^(\d{1,2})([LRC])?$")


class OurAirportsError(Exception):
    """CSV fetch/parse failed on every attempt."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class AirportNotFoundError(OurAirportsError):
    """ICAO code not present in airports.csv."""


def heading_from_ident(ident: str) -> float | None:
    """Runway ident (``13L``/``31``/``02C``) → magnetic-ish heading degrees.

    Returns None when the ident is not a plain runway number.
    """
    match = _IDENT_HEADING_RE.match(ident.strip().upper())
    if not match:
        return None
    number = int(match.group(1))
    if not 1 <= number <= 36:
        return None
    return float((number * 10) % 360)


def _fnum(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


async def _fetch_csv(
    name: str, client: httpx.AsyncClient | None = None
) -> tuple[str, bool]:
    """Return ``(csv_text, cache_hit)`` for ``airports|runways|airport-frequencies``."""
    key = cache_mod.airports_cache_key("ourairports", name)
    cached = await cache_mod.cache_get(key)
    if cached is not None:
        return cached, True
    url = f"{settings.ourairports_base_url}/{name}.csv"
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=settings.ourairports_timeout_s)
    try:
        resp = await client.get(
            url, headers={"User-Agent": settings.user_agent}
        )
        if resp.status_code != 200:
            raise OurAirportsError(
                f"{name}.csv unavailable (HTTP {resp.status_code}) — retry shortly."
            )
        text = resp.text
        # Sanity: must look like a CSV with a header row.
        first_line = text.splitlines()[0] if text else ""
        if "," not in first_line or "ident" not in first_line.lower():
            raise OurAirportsError(f"{name}.csv returned an unexpected payload.")
    finally:
        if own_client:
            await client.aclose()
    await cache_mod.cache_set(key, text)
    return text, False


def _rows(csv_text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(csv_text)))


async def resolve_airport(icao: str) -> tuple[dict, bool]:
    """Find the airport row for ``icao`` (upper-cased). Returns (row, cache_hit).

    Matches ``ident`` first, then ``gps_code`` (some fields are filed under
    a local ident with the ICAO in ``gps_code``), then ``iata_code`` so
    three-letter IATA codes resolve to their airport too.
    """
    text, hit = await _fetch_csv("airports")
    wanted = icao.strip().upper()
    fallback: dict | None = None
    iata_match: dict | None = None
    cache_hit = hit
    for row in _rows(text):
        ident = (row.get("ident") or "").strip().upper()
        if ident == wanted and (row.get("type") or "") != "closed":
            return row, cache_hit
        if fallback is None and (row.get("gps_code") or "").strip().upper() == wanted:
            fallback = row
        if iata_match is None and (row.get("iata_code") or "").strip().upper() == wanted:
            iata_match = row
    if fallback is not None:
        return fallback, cache_hit
    if iata_match is not None:
        return iata_match, cache_hit
    raise AirportNotFoundError(f"No airport found for ICAO '{wanted}'.")


_TYPE_RANK = {
    "large_airport": 0,
    "medium_airport": 1,
    "small_airport": 2,
    "seaplane_base": 3,
    "heliport": 4,
    "balloonport": 5,
}


def rank_candidates(
    rows: list[dict[str, str]], query: str, limit: int = 5
) -> list[dict]:
    """Rank airport rows against a freeform query (pure, no I/O).

    Score: exact ident/iata/gps (0) > ident/iata prefix (1) >
    name/municipality substring (2); ties break by airport size, then name.
    ``closed`` rows are excluded. Returns at most ``limit`` candidate dicts.
    """
    q = query.strip().upper()
    if not q:
        return []
    scored: list[tuple[tuple, dict]] = []
    for row in rows:
        if (row.get("type") or "") == "closed":
            continue
        ident = (row.get("ident") or "").strip().upper()
        iata = (row.get("iata_code") or "").strip().upper()
        gps = (row.get("gps_code") or "").strip().upper()
        name = (row.get("name") or "").strip()
        muni = (row.get("municipality") or "").strip()
        if q in (ident, iata, gps):
            score = 0
        elif ident.startswith(q) or (iata and iata.startswith(q)):
            score = 1
        elif q in name.upper() or (muni and q in muni.upper()):
            score = 2
        else:
            continue
        key = (score, _TYPE_RANK.get(row.get("type") or "", 9), name)
        scored.append((key, {
            "icao": ident,
            "iata": (row.get("iata_code") or "").strip(),
            "name": name,
            "municipality": muni,
            "iso_country": (row.get("iso_country") or "").strip(),
            "lat": _fnum(row.get("latitude_deg")),
            "lon": _fnum(row.get("longitude_deg")),
            "type": row.get("type") or "",
        }))
    scored.sort(key=lambda item: item[0])
    return [candidate for _, candidate in scored[: max(limit, 0)]]


async def search_airports(query: str, limit: int = 5) -> tuple[list[dict], bool]:
    """Freeform search over the cached ``airports.csv``.

    Returns ``(candidates, cache_hit)``. CSV parsing runs off the event
    loop (the file is ~12 MB).
    """
    import asyncio as _asyncio

    text, hit = await _fetch_csv("airports")
    rows = await _asyncio.to_thread(_rows, text)
    return rank_candidates(rows, query, limit), hit


async def airport_runways(airport_ident: str) -> tuple[list[dict], bool]:
    """All open runway rows for an ``airports.csv`` ident (both ends usable)."""
    text, hit = await _fetch_csv("runways")
    wanted = airport_ident.strip().upper()
    out = [
        row
        for row in _rows(text)
        if (row.get("airport_ident") or "").strip().upper() == wanted
        and (row.get("closed") or "0").strip() != "1"
    ]
    return out, hit


async def airport_frequencies(airport_ident: str) -> tuple[list[dict], bool]:
    """Grouped/deduplicated ``(type, description, frequency_mhz)`` rows."""
    text, hit = await _fetch_csv("airport-frequencies")
    wanted = airport_ident.strip().upper()
    seen: set[tuple[str, str, float]] = set()
    out: list[dict] = []
    for row in _rows(text):
        if (row.get("airport_ident") or "").strip().upper() != wanted:
            continue
        freq = _fnum(row.get("frequency_mhz"))
        if freq is None:
            continue
        key = (
            (row.get("type") or "").strip(),
            (row.get("description") or "").strip(),
            round(freq, 3),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {"type": key[0], "description": key[1], "frequency_mhz": key[2]}
        )
    # Stable order: by type then frequency (matches reference strip layout).
    out.sort(key=lambda r: (r["type"], r["frequency_mhz"]))
    return out, hit


def runway_endpoints(
    row: dict, center_lat: float, center_lon: float
) -> tuple[tuple[float, float], tuple[float, float], bool]:
    """``((le_lon, le_lat), (he_lon, he_lat), derived)`` for a runway row.

    Prefers ``le_/he_latitude_deg`` when both ends are present; otherwise
    derives endpoints from the airport center ± half length along the ident
    heading (spec fallback for small fields).
    """
    le_lat = _fnum(row.get("le_latitude_deg"))
    le_lon = _fnum(row.get("le_longitude_deg"))
    he_lat = _fnum(row.get("he_latitude_deg"))
    he_lon = _fnum(row.get("he_longitude_deg"))
    if None not in (le_lat, le_lon, he_lat, he_lon):
        return (le_lon, le_lat), (he_lon, he_lat), False  # type: ignore[misc]

    length_m = (_fnum(row.get("length_ft")) or 0.0) * FT_TO_M
    heading = heading_from_ident(row.get("le_ident") or "")
    if heading is None:
        heading = heading_from_ident(row.get("he_ident") or "")
    if heading is None or length_m <= 0:
        # Last resort: a short east-west stub so the runway still draws.
        length_m, heading = 800.0, 90.0
    half = length_m / 2.0
    import math

    rad = math.radians(heading)
    dx = math.sin(rad) * half  # meters east for the HE end
    dy = math.cos(rad) * half  # meters north for the HE end
    dlat = dy / _M_PER_DEG_LAT
    import math as _m

    dlon = dx / (_M_PER_DEG_LON_EQUATOR * _m.cos(_m.radians(center_lat)))
    le = (center_lon - dlon, center_lat - dlat)
    he = (center_lon + dlon, center_lat + dlat)
    return le, he, True


__all__ = [
    "AirportNotFoundError",
    "OurAirportsError",
    "airport_frequencies",
    "airport_runways",
    "heading_from_ident",
    "resolve_airport",
    "runway_endpoints",
]
