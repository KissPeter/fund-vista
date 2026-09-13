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

from pydantic import BaseModel, ConfigDict, Field, model_validator

MethodName = Literal["contour", "centerline", "hatch", "flow"]
PageSizeName = Literal["A4", "A3", "A5", "Letter", "a4", "a3", "a5", "letter"]
Orientation = Literal["portrait", "landscape"]

# Display-only pen color (physical pen). Allowlist enforced by
# backgrounds.LINE_COLORS at render time; any value here must be a key there.
LineColorName = Literal["black", "white", "red", "blue"]
# Display-only page background (paper texture preview). Vendored files in
# backend/penplot/backgrounds/ (white e-commerce frames pre-trimmed);
# "none" is plain paper. Independent of line_color — any combination works.
BackgroundName = Literal["none", "dark-texture", "light-texture"]


class PageParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    size: PageSizeName = "A4"
    orientation: Orientation = "portrait"
    margin_mm: float = Field(default=10.0, ge=0.0, le=50.0)
    padding_mm: float = Field(
        default=5.0, ge=0.0, le=20.0,
        description="Internal padding inside the margin/frame; artwork never touches the border.",
    )
    frame: bool = Field(
        default=False,
        description="Draw the whole-page margin frame (rounded border).",
    )
    frame_radius_mm: float = Field(
        default=2.0, ge=0.0, le=20.0,
        description="Corner radius of the page frame; 0 is sharp.",
    )


LabelAlign = Literal["left", "right", "fill"]
LabelFont = Literal[
    "futural", "futuram", "simplex",
    "excalifont", "comic-shanns", "nunito",
]


class LabelParams(BaseModel):
    """Blueprint title-block label (bottom strip).

    Faces mirror Drawscape's hershey-text select (futural default, futuram,
    simplex, all single-stroke) plus three hybrid outline faces (excalifont,
    comic-shanns, nunito: TTF outlines traced to polylines, so the pen draws
    each stem twice). futural/futuram cover ASCII 33-126 incl. lowercase;
    simplex covers A-Z 0-9 space + 18 marks; outline faces cover Latin text
    incl. accents. Anything else is skipped with a
    `label_unsupported_characters` warning.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    text: str = Field(default="", max_length=120)
    align: LabelAlign = "right"
    height_mm: float = Field(default=5.0, gt=0.0, le=25.0)
    font: LabelFont = "futural"
    border: bool = True
    border_radius_mm: float = Field(
        default=2.0, ge=0.0, le=20.0,
        description="Corner radius of the title-block frame; 0 is sharp.",
    )
    pad_left_mm: float = Field(
        default=0.0, ge=0.0, le=20.0,
        description="Text inset from the left frame vertical.",
    )
    pad_right_mm: float = Field(
        default=0.0, ge=0.0, le=20.0,
        description="Text inset from the right frame vertical.",
    )


class PenParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draw_speed_mm_s: float = Field(default=40.0, gt=0.0, le=500.0)
    travel_speed_mm_s: float = Field(default=100.0, gt=0.0, le=1000.0)
    pen_lift_s: float = Field(default=0.3, ge=0.0, le=10.0)


class ConvertParams(BaseModel):
    """Slider state. Defaults match the spec §2.3 example.

    Line generation is multi-select: ``methods`` lists every generator to run
    (in order — hatch shading first, contour outlines last is the classic
    combination). Outputs are concatenated before the shared optimize chain.
    The singular ``method`` is a deprecated alias kept for old clients: it
    maps to a one-element ``methods`` and is cleared afterwards so the
    canonical form (and the result cache key) is always ``methods``-only.
    """

    model_config = ConfigDict(extra="forbid")

    method: MethodName | None = Field(
        default=None,
        description="Deprecated alias for methods=[method].",
    )
    methods: list[MethodName] | None = Field(
        default=None, min_length=1,
        description="Generators to run, in order. Defaults to ['hatch'].",
    )
    threshold: int = Field(default=128, ge=0, le=255)
    blur_radius: float = Field(default=1.0, ge=0.0, le=10.0)
    contrast: float = Field(
        default=1.0, ge=0.0, le=4.0,
        description="Linear contrast stretch before thresholding; 1.0 is neutral.",
    )
    brightness: float = Field(
        default=0.0, ge=-255.0, le=255.0,
        description="Additive brightness offset after contrast; 0 is neutral.",
    )
    remove_background: bool = Field(
        default=False,
        description="Flatten uneven paper background before thresholding.",
    )
    strip_hatch_px: int = Field(
        default=0, ge=0, le=50,
        description=(
            "Erase ink strokes thinner than this many pixels (source "
            "resolution) via morphological opening on the ink mask, applied "
            "after thresholding and before line generation. Removes "
            "hatch/cross-hatch texture — decorative or baked into the "
            "source image's own shading — while leaving bolder strokes "
            "(outlines, solid fills) intact; unlike remove_background this "
            "is texture-width-based, not a background/foreground guess. "
            "0 disables."
        ),
    )
    hatch_pitch_mm: float = Field(default=1.2, gt=0.0, le=10.0)
    hatch_angle_deg: float = Field(
        default=45.0, ge=0.0, lt=180.0,
        description="Hatch line direction in degrees; cross pass runs at +90°.",
    )
    contour_simplify: float = Field(default=2.0, ge=0.0, le=20.0)
    centerline_prune_px: int = Field(
        default=4, ge=0, le=50,
        description=(
            "Centerline method only: drop skeleton branches shorter than this "
            "many pixels (source resolution) — thinning spurs from stroke "
            "edges and noise. 0 disables pruning. Ignored by other methods."
        ),
    )
    curve_smooth: int = Field(
        default=0, ge=0, le=3,
        description=(
            "Chaikin corner-cutting passes over the merged polylines (mm "
            "space), rounding faceted contour/centerline corners into smooth "
            "curves — the stand-in for potrace alphamax/opttolerance and "
            "vtracer spline mode. 0 disables (identity)."
        ),
    )
    linemerge_tolerance_mm: float = Field(default=0.5, ge=0.0, le=5.0)
    linesimplify_tolerance_mm: float = Field(default=0.1, ge=0.0, le=2.0)
    linesort: bool = True
    reloop_tolerance_mm: float = Field(default=0.05, ge=0.0, le=2.0)
    page: PageParams = Field(default_factory=PageParams)
    pen: PenParams = Field(default_factory=PenParams)
    label: LabelParams = Field(default_factory=LabelParams)
    line_color: LineColorName = Field(
        default="black",
        description=(
            "Display pen color (black/white/red/blue). Preview + SVG stroke "
            "only; never affects geometry or stats. Independent of background."
        ),
    )
    background: BackgroundName = Field(
        default="none",
        description=(
            "Display page background (none/dark-texture/light-texture). "
            "Vendored texture, white shop frame pre-trimmed, stretched to the "
            "full page. Preview + SVG fill layer only; never affects geometry "
            "or stats. Independent of line_color."
        ),
    )

    @model_validator(mode="after")
    def _resolve_methods(self) -> ConvertParams:
        """Canonicalize to ``methods``-only (deduped, order-preserving)."""
        if self.methods is None:
            self.methods = [self.method] if self.method is not None else ["hatch"]
        elif self.method is not None:
            raise ValueError("Use either 'methods' or legacy 'method', not both.")
        seen: list[str] = []
        for m in self.methods:
            if m not in seen:
                seen.append(m)
        self.methods = seen  # type: ignore[assignment]
        self.method = None  # alias consumed; keeps model_dump (cache key) canonical
        return self


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
