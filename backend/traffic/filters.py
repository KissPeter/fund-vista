"""Filter semantics for the traffic map (shared by the ingest job now, API/UI later).

Pure port of the ``tools/airport_traffic/filters.py`` POC — semantics
unchanged (plan §3):

``arrivals`` / ``departures`` select whole movements; ``holding`` /
``go_around`` *also* select every movement carrying that flag, so "only
go-arounds" = arrivals off, departures off, go-around on. Flagged movements
are drawn in the highlight pen only while their own flag is on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_WINDOW_S = 24 * 3600

# readsb/tar1090 ``dbFlags`` bits (verified on the 2026-10-10 archive)
DB_MILITARY, DB_INTERESTING, DB_PIA, DB_LADD = 1, 2, 4, 8


@dataclass
class Filters:
    t_from: float
    t_to: float
    arrivals: bool = True
    departures: bool = True
    holding: bool = True
    go_around: bool = True
    types: set[str] = field(default_factory=set)       # ICAO type codes, e.g. {"A320","B738"}
    airlines: set[str] = field(default_factory=set)    # callsign prefixes, e.g. {"WZZ","RYR"}
    callsign: str = ""                                 # substring match
    exclude_military: bool = True                      # owner decision 2026-10-11: military out by default
    exclude_privacy: bool = True                       # PIA / LADD flagged aircraft (privacy hygiene)

    def __post_init__(self) -> None:
        if self.t_to <= self.t_from:
            raise ValueError("end must be after start")
        if self.t_to - self.t_from > MAX_WINDOW_S:
            raise ValueError("window longer than 24 h")


def pen(f: dict, flt: Filters) -> str | None:
    """Layer a movement is drawn in (``None`` = hidden)."""
    if not (flt.t_from <= f["t"] < flt.t_to):
        return None
    flags = f.get("db_flags") or 0
    if flt.exclude_military and flags & DB_MILITARY:
        return None
    if flt.exclude_privacy and flags & (DB_PIA | DB_LADD):
        return None
    if flt.types and (f.get("type") or "") not in flt.types:
        return None
    cs = (f.get("flight") or "").upper()
    if flt.airlines and not any(cs.startswith(a.upper()) for a in flt.airlines):
        return None
    if flt.callsign and flt.callsign.upper() not in cs:
        return None
    selected = (f["kind"] == "arr" and flt.arrivals) or (f["kind"] == "dep" and flt.departures) \
        or (f["go_around"] and flt.go_around) or (f["holding"] and flt.holding)
    if not selected:
        return None
    if f["go_around"] and flt.go_around:
        return "go_around"
    if f["holding"] and flt.holding:
        return "holding"
    return "arrivals" if f["kind"] == "arr" else "departures"
