"""Wire schemas for the traffic module (Phase 1: ingest + availability).

Additive contract — sync airports/citymap/convert paths are untouched.
``extra="forbid"`` on every request model (plan §6.4).
"""

from __future__ import annotations

import datetime as dt
import re

from pydantic import BaseModel, ConfigDict, field_validator

from backend.traffic.classify import CLASSIFIER_VERSION
from backend.traffic.hungary import MESSAGE as _HU_MESSAGE

ATTRIBUTION = (
    "Contains data from adsb.lol and OpenStreetMap contributors (ODbL); "
    "airport data by OurAirports (CC0). ADS-B coverage near the ground varies "
    "with receiver placement."
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TrafficIngestRequest(BaseModel):
    """Enqueue a day-load: ``POST /v1/jobs {type:"traffic_ingest", request}``."""

    model_config = ConfigDict(extra="forbid")

    icao: str
    date: str  # YYYY-MM-DD (UTC day in the adsb.lol archive)

    @field_validator("icao")
    @classmethod
    def _upper_icao(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if not v or len(v) > 8:
            raise ValueError("icao must be 1-8 characters")
        if not v.startswith("LH"):
            raise ValueError(_HU_MESSAGE)
        return v

    @field_validator("date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not _DATE_RE.match(v or ""):
            raise ValueError("date must be YYYY-MM-DD")
        try:
            dt.date.fromisoformat(v)
        except ValueError:
            raise ValueError("date must be YYYY-MM-DD")
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


__all__ = [
    "ATTRIBUTION",
    "AvailabilityResponse",
    "TrafficIngestRequest",
    "movements_key",
]
