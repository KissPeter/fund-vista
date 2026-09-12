"""Pydantic wire schemas for /v1. Pydantic is the single validation gate.

Conventions:
- Field ranges mirror the spec example (§2.3) with safe clamps so a slider
  can't send the pipeline into an infinite loop (e.g. hatch_pitch_mm > 0).
- ``extra="forbid"`` on params: a typo'd slider name fails loudly (422
  invalid_params) instead of being silently ignored.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MethodName = Literal["contour", "hatch", "flow"]
PageSizeName = Literal["A4", "A3", "A5", "Letter", "a4", "a3", "a5", "letter"]
Orientation = Literal["portrait", "landscape"]


class PageParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    size: PageSizeName = "A4"
    orientation: Orientation = "portrait"
    margin_mm: float = Field(default=10.0, ge=0.0, le=50.0)


class PenParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draw_speed_mm_s: float = Field(default=40.0, gt=0.0, le=500.0)
    travel_speed_mm_s: float = Field(default=100.0, gt=0.0, le=1000.0)
    pen_lift_s: float = Field(default=0.3, ge=0.0, le=10.0)


class ConvertParams(BaseModel):
    """Slider state. Defaults match the spec §2.3 example."""

    model_config = ConfigDict(extra="forbid")

    method: MethodName = "hatch"
    threshold: int = Field(default=128, ge=0, le=255)
    blur_radius: float = Field(default=1.0, ge=0.0, le=10.0)
    hatch_pitch_mm: float = Field(default=1.2, gt=0.0, le=10.0)
    contour_simplify: float = Field(default=2.0, ge=0.0, le=20.0)
    linemerge_tolerance_mm: float = Field(default=0.5, ge=0.0, le=5.0)
    linesimplify_tolerance_mm: float = Field(default=0.1, ge=0.0, le=2.0)
    linesort: bool = True
    reloop_tolerance_mm: float = Field(default=0.05, ge=0.0, le=2.0)
    page: PageParams = Field(default_factory=PageParams)
    pen: PenParams = Field(default_factory=PenParams)


class ConvertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Hex sha256 from GET /v1/images + POST /v1/images responses. Uppercase is
    # accepted (C.2.7) and normalized to lowercase by the router, so the
    # normalisation there is functional, not dead code.
    image_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    params: ConvertParams = Field(default_factory=ConvertParams)


class ImageMetaResponse(BaseModel):
    image_id: str
    format: str
    width: int
    height: int
    is_vector: bool
    warnings: list[str] = Field(default_factory=list)
    expires_at: datetime


class PointsStats(BaseModel):
    before: int
    after: int


class SegmentsStats(BaseModel):
    before: int
    after: int


class ConvertStats(BaseModel):
    points: PointsStats
    segments: SegmentsStats
    strokes: int
    pen_down_mm: float
    pen_up_mm: float
    estimated_time_s: float


class ConvertResponse(BaseModel):
    image_id: str
    svg_url: str
    # Equivalent vpype recipe (same stages/tolerances), NOT byte-reproducing —
    # see backend/penplot/SPEC_V1.md, "Implementation & divergence log".
    vpype_command: str
    stats: ConvertStats
    warnings: list[str] = Field(default_factory=list)
