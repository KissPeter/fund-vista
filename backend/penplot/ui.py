"""Server-rendered UI for the pen-plot v1 API (Jinja2 templates).

One page (`GET /penplot`, registered ahead of the catch-all proxy in
`backend/main.py`): file picker, slider form matching `ConvertParams`, live
SVG preview plus stats/warnings/`vpype_command`. All data flows through the
versioned JSON endpoints — the page itself renders no image math:

- defaults, method list and page sizes are injected server-side so the form
  can never drift from `schemas.ConvertParams`;
- upload-then-convert with the §4.3 silent re-upload retry lives in a small
  vanilla-JS block (debounced converts, no build step, no extra dependency).

The page is deliberately unthrottled: it renders a static template (no CPU
work), while every expensive call it makes is already rate-limited.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backend.penplot.config import PAGE_SIZES_MM
from backend.penplot.labels import LABEL_FONTS
from backend.penplot.schemas import ConvertParams

ui_router = APIRouter(tags=["penplot-ui"])

_templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)


@ui_router.get("/penplot", response_class=HTMLResponse)
async def penplot_ui(request: Request) -> HTMLResponse:
    """Render the converter page with code-accurate defaults."""
    defaults = ConvertParams().model_dump(mode="json")
    return _templates.TemplateResponse(
        request,
        "penplot.html",
        {
            "defaults": defaults,
            "methods": ["contour", "hatch", "flow"],
            "page_sizes": sorted(PAGE_SIZES_MM),
            "orientations": ["portrait", "landscape"],
            "fonts": list(LABEL_FONTS),
        },
    )
