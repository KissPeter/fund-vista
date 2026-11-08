"""Response and request models for the weather endpoints.

Phase 1: availability. Phase 2: render and import (same request body).
Every request model is ``extra="forbid"``.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.weather.config import (
    ALLOWED_HOURS,
    CITIES,
    DEFAULT_CITIES,
    DEFAULT_LAYERS,
    LAYER_IDS,
    REGIONS,
)

#: Request list caps: every layer once, and a sane number of cities.
MAX_LAYERS = len(LAYER_IDS)
MAX_CITIES = 12


class NearestPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str
    time: int


class AvailabilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["available", "out_of_range"]
    nearest: NearestPoint | None = None


class RenderRequest(BaseModel):
    """One weather map: the UTC analysis moment, the region, the sheet and the layers."""

    model_config = ConfigDict(extra="forbid")

    date: str = Field(..., description="UTC analysis day, YYYY-MM-DD.")
    time: int = Field(12, description="UTC analysis hour; offered: 0, 6, 12, 18.")
    region: str = Field("europe", description="Region preset.")
    orientation: Literal["landscape", "portrait"] = "landscape"
    layers: list[str] = Field(default_factory=lambda: list(DEFAULT_LAYERS),
                              min_length=1, max_length=MAX_LAYERS)
    cities: list[str] = Field(default_factory=lambda: list(DEFAULT_CITIES),
                              max_length=MAX_CITIES)

    @field_validator("date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        try:
            dt.date.fromisoformat(v)
        except ValueError as exc:
            raise ValueError("date must be YYYY-MM-DD") from exc
        return v

    @field_validator("time")
    @classmethod
    def _valid_time(cls, v: int) -> int:
        if v not in ALLOWED_HOURS:
            raise ValueError(f"time must be one of {list(ALLOWED_HOURS)}")
        return v

    @field_validator("region")
    @classmethod
    def _valid_region(cls, v: str) -> str:
        if v not in REGIONS:
            raise ValueError(f"unknown region {v!r}")
        return v

    @field_validator("layers")
    @classmethod
    def _valid_layers(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - set(LAYER_IDS))
        if unknown:
            raise ValueError(f"unknown layers: {unknown}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate layers are not allowed")
        return v

    @field_validator("cities")
    @classmethod
    def _valid_cities(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - set(CITIES))
        if unknown:
            raise ValueError(f"unknown cities: {unknown}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate cities are not allowed")
        return v


class RenderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str
    time: int
    region: str
    orientation: str
    layers: list[str]
    cities: list[str]
    svg_url: str
    stats: dict
    warnings: list[str]
    attribution: str
    cache_hit: bool


class ImportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_id: str
    date: str
    time: int
    layers: list[str]
    stats: dict
    warnings: list[str]
    attribution: str
