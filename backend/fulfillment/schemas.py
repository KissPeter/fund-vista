"""Wire models of the fulfillment ink-split endpoint (fund-vista #147).

The consumer is the shop's fulfillment step (shop #1079): it sends the
order's design token (HMAC, same triple the buyer flow minted), the render
params persisted on the WordPress order line (the shop half landed as
``CartItem.renderParams`` — a flat JSON-safe record that embeds its
``kind`` marker) and the order's ink assignments (the #923 runway and the
#1078 traffic layer inks). Everything is re-validated server-side; nothing
about money or the customer passes through here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.penplot.schemas import ConvertParams

#: The buyer-pickable inks (the runway ink set, issues #923/#1078) — every
#: assignment is one of these, or the Pen.
#: Green is the fourth ink of ``PAPER_PEN_MATRIX.white`` in the shop
#: (``src/lib/runway.ts`` ``RUNWAY_INK_HEX``), so a green runway / layer pick
#: is a valid order, not a 422.
InkName = Literal["black", "red", "blue", "green"]
#: The Pen itself may be white (electric-blue paper's single ink).
PenName = Literal["black", "red", "blue", "green", "white"]

Kind = Literal["airport", "traffic"]


class DesignToken(BaseModel):
    """The HMAC-signed design token triple (mirrors ``/v1/tokens``)."""

    model_config = ConfigDict(extra="forbid")

    design_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    exp: int = Field(gt=0)
    sig: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")


class InkAssignment(BaseModel):
    """The order's ink assignments: the Pen plus the explicit layer picks.

    Only *explicit* choices ride along (the #1078 cart contract): an absent
    runway / layer entry means "auto" — the layer uses the Pen ink.
    """

    model_config = ConfigDict(extra="forbid")

    pen: PenName
    runway: InkName | None = None
    layer_colors: dict[str, InkName] | None = None

    @field_validator("layer_colors")
    @classmethod
    def _known_layers(cls, v: dict[str, InkName] | None) -> dict[str, InkName] | None:
        if v is None:
            return v
        from backend.fulfillment.ink_split import TRAFFIC_WIRE_TO_SUFFIX

        unknown = sorted(set(v) - set(TRAFFIC_WIRE_TO_SUFFIX))
        if unknown:
            raise ValueError(f"unknown traffic layer id(s): {', '.join(unknown)}")
        return v


class InkSplitRequest(BaseModel):
    """Fulfillment request: token + kind + persisted render params + inks."""

    model_config = ConfigDict(extra="forbid")

    design: DesignToken
    kind: Kind
    #: The persisted render body (``CartItem.renderParams``). Validated
    #: against the kind's ``RenderRequest`` (``extra="forbid"``); the record's
    #: own ``kind`` marker key is tolerated and must agree.
    params: dict[str, object]
    inks: InkAssignment
    #: The convert params of the buyer's framed preview (page, title label,
    #: tables). When set, every pass is laid out on the sheet exactly like the
    #: preview and the Pen pass carries the frame, title band and tables, so
    #: the printable file matches what the buyer saw. Absent = the raw draft
    #: groups (legacy behaviour).
    convert: ConvertParams | None = None

    @field_validator("params")
    @classmethod
    def _params_shape(cls, v: dict[str, object]) -> dict[str, object]:
        marker = v.get("kind")
        if marker is not None and marker not in ("airport", "traffic"):
            raise ValueError("params.kind must be 'airport' or 'traffic'")
        return v

    @model_validator(mode="after")
    def _kind_specific(self) -> "InkSplitRequest":
        if self.kind == "airport" and self.inks.layer_colors:
            raise ValueError("layer colors belong to the traffic design")
        if self.kind == "traffic" and self.inks.runway is not None:
            raise ValueError("runway ink belongs to the airport design")
        return self


class InkFileOut(BaseModel):
    """One pen pass ready for the plotter."""

    model_config = ConfigDict(extra="forbid")

    ink: str
    hex: str
    layers: list[str]
    svg: str


class InkSplitResponse(BaseModel):
    """One SVG per distinct ink, plus the split's provenance."""

    model_config = ConfigDict(extra="forbid")

    design_id: str
    kind: Kind
    cache_hit: bool
    files: list[InkFileOut]
    attribution: str
    warnings: list[str] = Field(default_factory=list)
