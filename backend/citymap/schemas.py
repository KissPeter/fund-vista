"""Pydantic wire schemas for /v1/citymap. Same conventions as penplot:
Pydantic is the single validation gate, ``extra="forbid"`` everywhere so a
typo'd field fails loudly with 422 ``invalid_params``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LayerName = Literal[
    "highways",
    "roads",
    "paths",
    "rails",
    "aeroway",
    "waterway",
    "water",
    "buildings",
    "ferry",
]

# ODbL credit lives here (response metadata + UI), never in the artwork:
# the SVG itself is chrome-free so the plotter draws only map lines.
ATTRIBUTION = "© OpenStreetMap contributors · ODbL 1.0 · https://osm.org/copyright"


class BBox(BaseModel):
    """Explicit area: decimal degrees, south < north and west < east."""

    model_config = ConfigDict(extra="forbid")

    south: float = Field(ge=-90.0, le=90.0)
    west: float = Field(ge=-180.0, le=180.0)
    north: float = Field(ge=-90.0, le=90.0)
    east: float = Field(ge=-180.0, le=180.0)

    @model_validator(mode="after")
    def _order(self) -> BBox:
        if not self.south < self.north:
            raise ValueError("bbox needs south < north")
        if not self.west < self.east:
            raise ValueError("bbox needs west < east")
        return self

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.south, self.west, self.north, self.east)


class RenderRequest(BaseModel):
    """Render a city map. Exactly one of ``city`` / ``bbox`` is required."""

    model_config = ConfigDict(extra="forbid")

    city: str | None = Field(default=None, min_length=1, max_length=120)
    bbox: BBox | None = None
    layers: list[LayerName] = Field(min_length=1)
    min_path_len_m: float = Field(
        default=10.0, ge=0.0, le=100.0,
        description="Drop projected polylines shorter than this (meters). "
        "Pen-plotter fast path, like city-roads' minLength option.",
    )
    rotation_deg: float = Field(
        default=0.0, ge=-180.0, le=180.0,
        description="Rotate the artwork clockwise around the area center "
        "before fitting to the page (degrees).",
    )
    width: int = Field(
        default=1000, ge=100, le=4000,
        description="SVG width in user units (plane meters scaled to fit).",
    )

    @model_validator(mode="after")
    def _source_and_layers(self) -> RenderRequest:
        if (self.city is None) == (self.bbox is None):
            raise ValueError("Provide exactly one of 'city' or 'bbox'.")
        if self.city is not None and not self.city.strip():
            raise ValueError("'city' must not be blank.")
        seen: list[str] = []
        for layer in self.layers:
            if layer not in seen:
                seen.append(layer)
        self.layers = seen  # type: ignore[assignment]
        return self


class LayerInfo(BaseModel):
    id: str
    label: str
    description: str


class LayersResponse(BaseModel):
    layers: list[LayerInfo]


class GeocodeResponse(BaseModel):
    city: str
    display_name: str
    bbox: BBox
    lat: float
    lon: float
    cache_hit: bool


class GeocodeCandidate(BaseModel):
    """One Nominatim match for a place-name search."""

    model_config = ConfigDict(extra="forbid")

    display_name: str
    bbox: BBox
    lat: float
    lon: float
    category: str = ""
    type: str = ""


class GeocodeSearchResponse(BaseModel):
    city: str
    candidates: list[GeocodeCandidate]
    cache_hit: bool


class RenderResponse(BaseModel):
    city: str | None
    display_name: str | None
    bbox: BBox
    layers: list[str]
    path_counts: dict[str, int]
    raw_counts: dict[str, int]
    svg_url: str
    attribution: str = ATTRIBUTION
    warnings: list[str] = Field(default_factory=list)
    cache_hit: bool


class ImportResponse(BaseModel):
    """A city map registered as a penplot image — feed ``image_id`` to
    ``POST /v1/convert`` like an uploaded SVG (vector branch: shading
    sliders are ignored, pen/page/label/display all apply)."""

    image_id: str
    city: str | None
    display_name: str | None
    bbox: BBox
    layers: list[str]
    path_counts: dict[str, int]
    raw_counts: dict[str, int]
    attribution: str = ATTRIBUTION
    warnings: list[str] = Field(default_factory=list)
