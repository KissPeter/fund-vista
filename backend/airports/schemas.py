"""Pydantic wire schemas for /v1/airports. Same conventions as citymap:
Pydantic is the single validation gate, ``extra="forbid"`` everywhere so a
typo'd field fails loudly with 422 ``invalid_params``.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ICAO_RE = re.compile(r"^[A-Z0-9]{3,4}$")

# Produced-work credit: response metadata + SVG footer (the SVG footer IS the
# artwork here, unlike chrome-free citymap output).
ATTRIBUTION = (
    "Runway/frequency data: OurAirports.com (CC0). "
    "Ground layout: © OpenStreetMap contributors (ODbL)."
)


def normalize_icao(value: str) -> str:
    """Upper/strip an ICAO code, raising ValueError when malformed."""
    code = value.strip().upper()
    if not _ICAO_RE.match(code):
        raise ValueError("ICAO code must be 3–4 alphanumeric characters (e.g. LHBP).")
    return code


class RenderRequest(BaseModel):
    """Render an airport ground diagram. ``icao`` is the only required field."""

    model_config = ConfigDict(extra="forbid")

    icao: str = Field(min_length=3, max_length=4)
    radius_m: float = Field(
        default=3000.0, ge=500.0, le=6000.0,
        description="Overpass 'around' radius in meters from the airport center.",
    )
    min_path_len_m: float = Field(
        default=5.0, ge=0.0, le=100.0,
        description="Drop projected OSM polylines shorter than this (meters).",
    )
    width: int = Field(
        default=1000, ge=100, le=4000,
        description="SVG width in user units (plane meters scaled to fit).",
    )
    context: bool = Field(
        default=False,
        description="Also draw surrounding streets/buildings/water (second "
        "Overpass query, faintest group under the airfield geometry).",
    )
    zoom: float = Field(
        default=1.0, ge=0.25, le=4.0,
        description="Zoom about the scene center (1.0 = fit all content; "
        ">1 crops edges to fill the page, <1 adds margin).",
    )

    _norm_icao = field_validator("icao", mode="before")(normalize_icao)


class RunwayInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    le_ident: str
    he_ident: str
    length_ft: float | None = None
    width_ft: float | None = None
    surface: str = ""
    le_heading_deg: float | None = None
    he_heading_deg: float | None = None
    endpoints_derived: bool = False


class FrequencyInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    description: str = ""
    frequency_mhz: float


class LookupResponse(BaseModel):
    icao: str
    name: str
    municipality: str = ""
    iso_country: str = ""
    latitude_deg: float
    longitude_deg: float
    elevation_ft: float | None = None
    iata: str = ""
    runways: list[RunwayInfo] = Field(default_factory=list)
    frequencies: list[FrequencyInfo] = Field(default_factory=list)
    attribution: str = ATTRIBUTION
    warnings: list[str] = Field(default_factory=list)
    cache_hit: bool = False


class RenderResponse(BaseModel):
    icao: str
    name: str
    runways: list[RunwayInfo] = Field(default_factory=list)
    frequencies: list[FrequencyInfo] = Field(default_factory=list)
    rotation_deg: float
    path_counts: dict[str, int]
    raw_counts: dict[str, int]
    svg_url: str
    attribution: str = ATTRIBUTION
    warnings: list[str] = Field(default_factory=list)
    cache_hit: bool = False


class ImportResponse(BaseModel):
    """An airport diagram registered as a penplot image — feed ``image_id``
    to ``POST /v1/convert`` like an uploaded SVG (vector branch)."""

    image_id: str
    icao: str
    name: str
    rotation_deg: float
    path_counts: dict[str, int]
    raw_counts: dict[str, int]
    attribution: str = ATTRIBUTION
    warnings: list[str] = Field(default_factory=list)


class SearchCandidate(BaseModel):
    """One OurAirports match for a freeform query."""

    model_config = ConfigDict(extra="forbid")

    icao: str
    iata: str = ""
    name: str
    municipality: str = ""
    iso_country: str = ""
    lat: float | None = None
    lon: float | None = None
    type: str = ""


class SearchResponse(BaseModel):
    query: str
    candidates: list[SearchCandidate]
    cache_hit: bool = False
