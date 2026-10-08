"""Pydantic wire schemas for /v1/wordmark. Same conventions as
airports/citymap: Pydantic is the single validation gate,
``extra="forbid"`` everywhere so a typo'd field fails loudly with 422
``invalid_params``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Rendered faces. v1 ships one vendored OFL face; the Literal stays so new
#: faces extend the API without reshaping it.
FontName = Literal["poppins-bold"]

#: Solid = letters unioned (the reference default); knockout = neighbouring
#: overlaps flip to paper (the reference ``--xor``); layered = each letter
#: sits on the previous ones behind a thin gap (``--layered``).
WordmarkMode = Literal["solid", "knockout", "layered"]

FONTS: dict[str, str] = {
    "poppins-bold": "Poppins-Bold.ttf",
}

# Produced-work credit: response metadata + SVG footer comment.
ATTRIBUTION = (
    "Wordmark set in Poppins Bold (SIL OFL 1.1), "
    "font vendored under backend/wordmark/fonts/."
)


class CutSpec(BaseModel):
    """One dot cut: the junction (gap between letter ``junction - 1`` and
    letter ``junction``), the vertical position (0 = baseline, 1 = cap
    height) and the core radius as a fraction of the cap height."""

    model_config = ConfigDict(extra="forbid")

    junction: int = Field(ge=1, description="Gap index between letters.")
    y: float = Field(ge=0.0, le=1.0, description="Height, 0 = baseline, 1 = cap height.")
    r: float = Field(ge=0.05, le=0.4, description="Core radius, cap-height units.")


class RenderRequest(BaseModel):
    """Render a negative-space wordmark. ``text`` is the only required field."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=24)
    font: FontName = Field(default="poppins-bold")
    mode: WordmarkMode = Field(default="solid")
    tracking: float = Field(
        default=-0.12, ge=-0.3, le=0.5,
        description="Letter spacing in em units (negative = tighter).",
    )
    gap: float = Field(
        default=0.04, ge=0.01, le=0.15,
        description="Gap-ring width around each cut, cap-height units.",
    )
    cuts: list[CutSpec] | None = Field(
        default=None,
        description="Explicit cuts (omit = deterministic cuts derived from the text).",
    )
    width: int = Field(
        default=1000, ge=100, le=4000,
        description="SVG width in user units (design scaled to fit).",
    )

    @model_validator(mode="after")
    def _check_cuts(self) -> RenderRequest:
        if self.cuts is not None:
            if not self.cuts:
                raise ValueError("'cuts' must not be empty (omit for automatic cuts).")
            if len(self.cuts) > 12:
                raise ValueError("'cuts' holds at most 12 entries.")
            top = len(self.text) - 1
            for cut in self.cuts:
                if cut.junction > top:
                    raise ValueError(
                        f"cut junction {cut.junction} is past the last gap "
                        f"(text has {top} gaps)."
                    )
        return self


class RenderResponse(BaseModel):
    text: str
    font: str
    mode: str
    cuts_applied: list[CutSpec] = Field(default_factory=list)
    path_counts: dict[str, int]
    raw_counts: dict[str, int]
    svg_url: str
    attribution: str = ATTRIBUTION
    warnings: list[str] = Field(default_factory=list)
    cache_hit: bool = False


class ImportResponse(BaseModel):
    """A wordmark registered as a penplot image — feed ``image_id`` to
    ``POST /v1/convert`` like an uploaded SVG (vector branch)."""

    image_id: str
    text: str
    font: str
    mode: str
    cuts_applied: list[CutSpec] = Field(default_factory=list)
    path_counts: dict[str, int]
    raw_counts: dict[str, int]
    attribution: str = ATTRIBUTION
    warnings: list[str] = Field(default_factory=list)
