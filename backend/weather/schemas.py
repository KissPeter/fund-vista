"""Response models for the weather endpoints (Phase 1: availability only)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class NearestPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str
    time: int


class AvailabilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["available", "out_of_range"]
    nearest: NearestPoint | None = None
