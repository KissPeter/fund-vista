"""Wire schemas for the traffic module (Phase 1: ingest + availability;
Phase 2: render + import).

Additive contract — sync airports/citymap/convert paths are untouched.
``extra="forbid"`` on every request model (plan §6.4).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.traffic.aircraft import CATEGORIES
from backend.traffic.classify import CLASSIFIER_VERSION
from backend.traffic.filters import Filters
from backend.traffic.hungary import MESSAGE as _HU_MESSAGE

ATTRIBUTION = (
    "Contains data from adsb.lol and OpenStreetMap contributors (ODbL); "
    "airport data by OurAirports (CC0). ADS-B coverage near the ground varies "
    "with receiver placement."
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$|^24:00$")
_FLOW_RE = re.compile(r"^(0[1-9]|[12]\d|3[0-6])$")  # runway numbers 01..36


def _normalize_icao(v: str) -> str:
    v = (v or "").strip().upper()
    if not v or len(v) > 8:
        raise ValueError("icao must be 1-8 characters")
    if not v.startswith("LH"):
        raise ValueError(_HU_MESSAGE)
    return v


def _valid_day(v: str) -> str:
    if not _DATE_RE.match(v or ""):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        dt.date.fromisoformat(v)
    except ValueError:
        raise ValueError("date must be YYYY-MM-DD")
    return v


def _hhmm_to_min(v: str) -> int:
    """HH:MM → minutes since midnight; ``24:00`` = end of day (1440)."""
    if not _TIME_RE.match(v or ""):
        raise ValueError("time must be HH:MM (t_to may be 24:00 for end of day)")
    h, m = v.split(":")
    return int(h) * 60 + int(m)


class TrafficIngestRequest(BaseModel):
    """Enqueue a day-load: ``POST /v1/jobs {type:"traffic_ingest", request}``.

    Either ``icao`` (legacy single-airport shape, unchanged response) or
    ``icaos`` (one archive pass for the whole list; response carries a
    per-airport ``airports`` list). Both set, both empty, or an empty
    ``icaos`` entry is a 422.
    """

    model_config = ConfigDict(extra="forbid")

    icao: str = ""
    icaos: list[str] = Field(default_factory=list)
    date: str  # YYYY-MM-DD (UTC day in the adsb.lol archive)

    @field_validator("icao", "icaos")
    @classmethod
    def _upper_icao(cls, v):
        if isinstance(v, str):
            return _normalize_icao(v)
        return [_normalize_icao(x) for x in v]

    @field_validator("icaos")
    @classmethod
    def _icaos_cap(cls, v: list[str]) -> list[str]:
        if len(v) > 100:
            raise ValueError("icaos supports at most 100 airports per job")
        return v

    @field_validator("date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        return _valid_day(v)

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> "TrafficIngestRequest":
        if bool(self.icao) == bool(self.icaos):
            raise ValueError("use exactly one of 'icao' or 'icaos'")
        return self


class IcaoQuery(BaseModel):
    """ICAO query parameter (``GET /v1/traffic/availability``).

    Pydantic owns the format rule: 1-8 characters, upper-cased. The
    Hungary-only rule stays a router decision — it has its own error code
    (``unsupported_airport``), not ``invalid_params``.
    """

    model_config = ConfigDict(extra="forbid")

    icao: str

    @field_validator("icao")
    @classmethod
    def _check_icao(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if not v or len(v) > 8:
            raise ValueError("icao must be 1-8 characters.")
        return v


class DayQuery(BaseModel):
    """Day query parameter — ISO ``YYYY-MM-DD`` (the archive's UTC day)."""

    model_config = ConfigDict(extra="forbid")

    date: str

    @field_validator("date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        # No strip: the day is an exact ISO date on the wire, a padded value
        # is a client bug (422), not something to guess around.
        try:
            dt.date.fromisoformat(v)
        except ValueError:
            raise ValueError("date must be YYYY-MM-DD.") from None
        return v


def movements_key(icao: str, day_iso: str) -> str:
    """L2 cache-kind parts (key built by :mod:`backend.traffic.cache`)."""
    return f"{icao.upper()}:{day_iso}:{CLASSIFIER_VERSION}"


class AvailabilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    tag: str | None = None
    bytes: int | None = None
    parts: int | None = None
    reason: str | None = None
    min: str | None = None
    max: str | None = None


class RenderRequest(BaseModel):
    """Render the traffic map for one airport/day + UTC window (Phase 2).

    ``view_*`` is all-or-none (omit = whole ingest bbox); the view must lie
    inside the ingest bbox (plan §6.4: max view = ingest bbox). ``t_to``
    accepts ``24:00`` for end of day; the window is ``[t_from, t_to)``.

    ``map`` / ``airport`` are the two extra layer switches (issue #1080):
    the basemap groups (motorways / waterways / water) and the airfield
    group + the airport marker circle. Both default to on.
    """

    model_config = ConfigDict(extra="forbid")

    icao: str
    date: str  # YYYY-MM-DD (UTC day, must already be ingested → else 409)
    t_from: str = "00:00"
    t_to: str = "24:00"
    arrivals: bool = True
    departures: bool = True
    holding: bool = True
    go_around: bool = True
    map: bool = True  # issue #1080: the basemap groups (country/motorways/waterways/water)
    airport: bool = True  # issue #1080: the airfield group + the airport marker circle
    types: list[str] = Field(default_factory=list)
    airlines: list[str] = Field(default_factory=list)
    callsign: str = ""
    type_categories: list[str] = Field(default_factory=list)  # Phase 5: wide/narrow/regional/other
    flows: list[str] = Field(default_factory=list)            # Phase 5: runway numbers "13"
    alt_max_agl_ft: float | None = Field(default=None, gt=0, le=60000)  # Phase 5: noise-map band
    pen_mode: Literal["kind", "category"] = "kind"            # Phase 5: pen grouping
    exclude_military: bool = True
    exclude_privacy: bool = True
    view_south: float | None = None
    view_north: float | None = None
    view_west: float | None = None
    view_east: float | None = None
    width: int = Field(default=1000, ge=100, le=4000)
    min_path_len_m: float = Field(
        default=0.0, ge=0.0, le=100.0,
        description="Drop projected polylines shorter than this (meters). "
        "Pen-plotter fast path, like city-roads' minLength option; "
        "applies to every layer, basemap and movements alike.",
    )
    viewport_aspect: float | None = Field(
        default=None, gt=0.0, le=10.0,
        description="SVG canvas aspect (width/height) — the paper's shape. "
        "The view grows to match where the day bbox allows, the rest is "
        "framed fit-contain centred. Omit for the historical bbox ratio.",
    )

    @field_validator("icao")
    @classmethod
    def _upper_icao(cls, v: str) -> str:
        return _normalize_icao(v)

    @field_validator("date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        return _valid_day(v)

    @field_validator("type_categories")
    @classmethod
    def _known_categories(cls, v: list[str]) -> list[str]:
        bad = [c for c in v if c not in CATEGORIES]
        if bad:
            raise ValueError(f"unknown aircraft categories: {bad} (want {list(CATEGORIES)})")
        return v

    @field_validator("flows")
    @classmethod
    def _known_flows(cls, v: list[str]) -> list[str]:
        bad = [f for f in v if not _FLOW_RE.match(f or "")]
        if bad:
            raise ValueError(f"flows must be runway numbers 01..36: {bad}")
        return v

    @model_validator(mode="after")
    def _check_window_and_view(self) -> RenderRequest:
        f, t = _hhmm_to_min(self.t_from), _hhmm_to_min(self.t_to)
        if t <= f:
            raise ValueError("t_to must be after t_from")
        if t - f > 24 * 60:
            raise ValueError("window longer than 24 h")
        vs = (self.view_south, self.view_north, self.view_west, self.view_east)
        if any(v is None for v in vs) and not all(v is None for v in vs):
            raise ValueError("view_* must be all set or all omitted")
        if self.view_south is not None and self.view_north is not None \
                and self.view_south >= self.view_north:
            raise ValueError("view_south must be below view_north")
        if self.view_west is not None and self.view_east is not None \
                and self.view_west >= self.view_east:
            raise ValueError("view_west must be below view_east")
        return self

    def day_range(self, day: dt.date) -> tuple[float, float]:
        """Window as epoch seconds ``(t_from, t_to)`` on the given UTC day."""
        base = dt.datetime.combine(day, dt.time(0), tzinfo=dt.timezone.utc).timestamp()
        return base + _hhmm_to_min(self.t_from) * 60, base + _hhmm_to_min(self.t_to) * 60

    def filters(self, t_from_ts: float, t_to_ts: float) -> Filters:
        """Filter set for this request (highlight model, plan §3)."""
        return Filters(
            t_from=t_from_ts, t_to=t_to_ts,
            arrivals=self.arrivals, departures=self.departures,
            holding=self.holding, go_around=self.go_around,
            types=set(self.types), airlines=set(self.airlines),
            callsign=self.callsign,
            type_categories=set(self.type_categories), flows=set(self.flows),
            alt_max_agl_ft=self.alt_max_agl_ft, pen_mode=self.pen_mode,
            exclude_military=self.exclude_military,
            exclude_privacy=self.exclude_privacy,
        )


class RenderResponse(BaseModel):
    icao: str
    date: str
    t_from: str
    t_to: str
    counts: dict[str, int]  # potential per layer (counted before the layer
    # toggles): kind mode arrivals/departures/…,
    # category mode wide/narrow/regional/other/…, always + holding/go_around
    movements: int  # classified movements in the window (before pen filtering)
    stats: dict = Field(default_factory=dict)  # movement_stats(window): movements,
    # arrivals/departures, go_arounds + rate, holdings + minutes, busiest_hour
    svg_url: str
    attribution: str = ATTRIBUTION
    warnings: list[str] = Field(default_factory=list)
    cache_hit: bool = False


class ImportResponse(BaseModel):
    """A traffic map registered as a penplot image — feed ``image_id``
    to ``POST /v1/convert`` like an uploaded SVG (vector branch)."""

    image_id: str
    icao: str
    date: str
    t_from: str
    t_to: str
    counts: dict[str, int]
    movements: int
    stats: dict = Field(default_factory=dict)  # same movement_stats(window) as render
    attribution: str = ATTRIBUTION
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "ATTRIBUTION",
    "AvailabilityResponse",
    "ImportResponse",
    "RenderRequest",
    "RenderResponse",
    "TrafficIngestRequest",
    "movements_key",
]
