"""Wire schemas of ``POST /v1/pcb/render`` (plan D7, D4, D6).

Pydantic is the validation gate; ``extra="forbid"`` everywhere so a typo'd
field fails with 422 ``invalid_params``.

Paper and pen rule (plan D7): A4 only; white ink on electric-blue paper and
the colour inks (black, blue, red) on white paper. There is no green. The
matrix below is the backend's copy of that rule: the shop's launch catalogue
(``PAPER_PEN_MATRIX`` in ``src/lib/penplot.params.ts``) also lists white ink
on black paper and green, which D7 does not allow for this kind.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.pcb.layers import SUBLAYER_IDS

Paper = Literal["white", "electric-blue"]
PenName = Literal["black", "blue", "red", "white"]
LayerInk = Literal["auto", "black", "blue", "red", "white"]
SubLayerId = Literal["pads", "tracks", "pours", "drills"]

#: Pens that may plot on each paper (plan D7).
PAPER_PENS: dict[str, frozenset[str]] = {
    "white": frozenset({"black", "blue", "red"}),
    "electric-blue": frozenset({"white"}),
}


class LayerSpec(BaseModel):
    """One sub-layer: on or off, and its ink (``auto`` rides the Pen)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    ink: LayerInk = "auto"


class PcbRenderParams(BaseModel):
    """The render request's ``params`` form field (JSON text).

    A sub-layer missing from ``layers`` is off. Only enabled sub-layers are
    checked against the paper: a disabled choice may stay on the order.
    """

    model_config = ConfigDict(extra="forbid")

    paper: Paper = "white"
    pen: PenName = "black"
    layers: dict[SubLayerId, LayerSpec] = Field(default_factory=dict)
    mirror: bool = False

    @model_validator(mode="after")
    def _paper_and_inks(self) -> PcbRenderParams:
        allowed = PAPER_PENS[self.paper]
        if self.pen not in allowed:
            raise ValueError(
                f"pen '{self.pen}' does not plot on {self.paper} paper "
                f"(allowed: {', '.join(sorted(allowed))})."
            )
        for layer, spec in self.layers.items():
            if spec.enabled and spec.ink != "auto" and spec.ink not in allowed:
                raise ValueError(
                    f"ink '{spec.ink}' for {layer} does not plot on {self.paper} paper."
                )
        return self

    def enabled_layers(self) -> dict[str, str]:
        """Enabled sub-layer id to resolved ink (``auto`` = the Pen), in order."""
        resolved: dict[str, str] = {}
        for layer in SUBLAYER_IDS:
            spec = self.layers.get(layer)
            if spec is not None and spec.enabled:
                resolved[layer] = self.pen if spec.ink == "auto" else spec.ink
        return resolved


class SheetStats(BaseModel):
    """Plot stats of the emitted strokes (same fields as the convert stats)."""

    strokes: int
    pen_down_mm: float
    pen_up_mm: float
    estimated_time_s: float


class LayerOut(BaseModel):
    id: str
    ink: str
    paths: int


class DesignToken(BaseModel):
    """The HMAC design token triple (same as ``POST /v1/tokens``)."""

    design_id: str
    exp: int
    sig: str


class PcbMetaResponse(BaseModel):
    """``GET /v1/pcb/results/{design_id}/meta``: stats and the render params."""

    stats: SheetStats
    params: PcbRenderParams


class PcbRenderResponse(BaseModel):
    svg: str
    svg_url: str
    stats: SheetStats
    layers: list[LayerOut]
    paper: str
    pen: str
    mirror: bool
    rotated: bool
    design: DesignToken | None = None
    warnings: list[str] = Field(default_factory=list)
    cache_hit: bool = False
