"""Pydantic wire schemas for /v1/airports. Same conventions as citymap:
Pydantic is the single validation gate, ``extra="forbid"`` everywhere so a
typo'd field fails loudly with 422 ``invalid_params``.
"""

from __future__ import annotations

import re

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Selectable diagram layers (airfield always fetched; context needs the
#: second Overpass query). Labels/badges/compass are annotations, not layers.
LayerName = Literal[
    "runway",
    "taxiway",
    "apron",
    "terminal",
    "hangar",
    "stands",
    "stopways",
    "highways",
    "roads",
    "paths",
    "rails",
    "waterway",
    "water",
    "buildings",
]

AIRFIELD_LAYERS: tuple[str, ...] = (
    "runway", "taxiway", "apron", "terminal", "hangar", "stands", "stopways",
)
CONTEXT_LAYERS: tuple[str, ...] = (
    "highways", "roads", "paths", "rails", "waterway", "water", "buildings",
)

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
    zoom: float = Field(
        default=1.0, ge=0.25, le=4.0,
        description="Zoom about the scene center (1.0 = fit all content; "
        ">1 crops edges to fill the page, <1 adds margin).",
    )
    layers: list[LayerName] | None = Field(
        default=None,
        description="Diagram layers to draw (omit = airfield default). "
        "Context layers fire the second Overpass query.",
    )

    _norm_icao = field_validator("icao", mode="before")(normalize_icao)

    @model_validator(mode="after")
    def _dedupe_layers(self) -> RenderRequest:
        if self.layers is not None:
            if not self.layers:
                raise ValueError("'layers' must not be empty (omit for default).")
            seen: list[str] = []
            for layer in self.layers:
                if layer not in seen:
                    seen.append(layer)
            self.layers = seen  # type: ignore[assignment]
        return self

    def effective_layers(self) -> list[str]:
        """Selected layers, or the airfield default when omitted."""
        return list(self.layers) if self.layers is not None else list(AIRFIELD_LAYERS)

    def needs_context(self) -> bool:
        """True when any selected layer needs the second Overpass query."""
        return any(layer in CONTEXT_LAYERS for layer in self.effective_layers())


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
    municipality: str = ""
    iso_country: str = ""
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
    municipality: str = ""
    iso_country: str = ""
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
