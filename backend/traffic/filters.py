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

from backend.traffic.aircraft import category_of
from backend.traffic.classify import ALT, GND, HOLD_EXCLUDE_KM, LAT, LON, T
from backend.traffic.geo import km_between

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
    type_categories: set[str] = field(default_factory=set)  # Phase 5: wide/narrow/regional/other
    flows: set[str] = field(default_factory=set)       # Phase 5: runway numbers, e.g. {"13","31"}
    alt_max_agl_ft: float | None = None                # Phase 5: noise-map band (min far AGL <= band)
    pen_mode: str = "kind"                             # Phase 5: "kind" | "category" pen grouping
    exclude_military: bool = True                      # owner decision 2026-10-11: military out by default
    exclude_privacy: bool = True                       # PIA / LADD flagged aircraft (privacy hygiene)

    def __post_init__(self) -> None:
        if self.t_to <= self.t_from:
            raise ValueError("end must be after start")
        if self.t_to - self.t_from > MAX_WINDOW_S:
            raise ValueError("window longer than 24 h")


def pen(f: dict, flt: Filters, airport: tuple[float, float, float] = ()) -> str | None:
    """Layer a movement is drawn in (``None`` = hidden).

    ``airport`` is ``(lat, lon, elev_ft)`` — required only when the Phase 5
    altitude band is set (AGL needs the field elevation); without it a
    banded filter hides everything rather than guessing.
    """
    if not (flt.t_from <= f["t"] < flt.t_to):
        return None
    flags = f.get("db_flags") or 0
    if flt.exclude_military and flags & DB_MILITARY:
        return None
    if flt.exclude_privacy and flags & (DB_PIA | DB_LADD):
        return None
    if flt.types and (f.get("type") or "") not in flt.types:
        return None
    if flt.type_categories and category_of(f.get("type")) not in flt.type_categories:
        return None
    if flt.flows and (f.get("flow") or "") not in flt.flows:
        return None
    if flt.alt_max_agl_ft is not None and not _low_enough(f, flt, airport):
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
    if flt.pen_mode == "category":
        return category_of(f.get("type"))
    return "arrivals" if f["kind"] == "arr" else "departures"


def _low_enough(f: dict, flt: Filters, airport: tuple[float, float, float]) -> bool:
    """Phase 5 altitude band: the movement's lowest in-window point outside
    the 12 km circuit must be at or below the band (AGL, ft).

    Touchdown is always ~0 AGL, so judging the whole leg would let every
    movement through — the circuit exclusion is what makes this a noise
    map. Legs with no points outside the circuit (purely local traffic)
    fall back to all in-window points, i.e. they pass.
    """
    if len(airport) != 3 or flt.alt_max_agl_ft is None:
        return False
    lat, lon, elev = airport
    in_window = [p for p in f["pts"] if flt.t_from <= p[T] < flt.t_to]
    far = [p for p in in_window if km_between(p[LAT], p[LON], lat, lon) > HOLD_EXCLUDE_KM]
    pool = far or in_window or f["pts"]
    if not pool:
        return True  # point-less movement draws nothing either way; don't hide it for altitude
    return min(0.0 if p[GND] else p[ALT] - elev for p in pool) <= flt.alt_max_agl_ft
