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

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from backend.pcb.layers import SUBLAYER_IDS
from backend.penplot.schemas import LabelParams, PageParams

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


#: Title lines: at most two (title, subtitle), each at most this long. PCB
#: keeps this 41-character cap independently of ``label.lines`` (52).
MAX_LINES = 2
MAX_LINE_CHARS = 41
_CONTROL = frozenset(chr(c) for c in [*range(0x20), 0x7F])


class PcbLayout(BaseModel):
    """The page furniture of a pcb sheet: the shop's Page tool, one to one.

    ``page`` and ``label`` are the penplot ``PageParams`` and ``LabelParams``
    models (same names, ranges and defaults). Only A4 is plotted. The label's
    ``enabled``, ``text`` and ``lines`` fields are not used here: the title
    travels in ``PcbRenderParams.lines``.
    """

    model_config = ConfigDict(extra="forbid")

    page: PageParams = Field(default_factory=PageParams)
    label: LabelParams = Field(default_factory=LabelParams)

    @field_validator("page")
    @classmethod
    def _a4_only(cls, page: PageParams) -> PageParams:
        if page.size.upper() != "A4":
            raise ValueError("only the A4 sheet is plotted for a pcb")
        return page


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
    #: Page furniture (orientation, frame, title label). None = today's sheet.
    layout: PcbLayout | None = None
    #: Title then subtitle. Blank entries are dropped; empty = no title.
    lines: list[str] = Field(default_factory=list)

    @field_validator("lines")
    @classmethod
    def _lines_ok(cls, lines: list[str]) -> list[str]:
        if len(lines) > MAX_LINES:
            raise ValueError("at most two lines (title, subtitle)")
        for line in lines:
            if len(line) > MAX_LINE_CHARS:
                raise ValueError(f"a line is at most {MAX_LINE_CHARS} characters")
            if any(ch in _CONTROL for ch in line):
                raise ValueError("control characters are not allowed in lines")
        return [line for line in lines if line.strip()]

    @model_serializer(mode="wrap")
    def _omit_absent_page(self, handler):
        """Leave an absent ``layout`` and empty ``lines`` out of the dump.

        The dump is the canonical params text (cache key, stored params, the
        ink-split match). A render without the page rail keeps the exact text
        it had before the fields existed, so a design cached before the
        change still matches its own params.
        """
        data = handler(self)
        if data.get("layout") is None:
            data.pop("layout", None)
        if not data.get("lines"):
            data.pop("lines", None)
        return data

    def has_page(self) -> bool:
        """True when the sheet carries page furniture or a title."""
        return self.layout is not None or bool(self.lines)

    @model_validator(mode="after")
    def _paper_and_inks(self) -> PcbRenderParams:
        if not self.lines and self.layout == PcbLayout():
            # A default layout without a title is today's sheet: store it as
            # absent, so both spellings share one canonical text.
            self.layout = None
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
