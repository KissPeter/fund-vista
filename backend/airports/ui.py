"""Server-rendered UI for airport diagrams (Jinja2 template).

One page (``GET /airports``): ICAO search → ``lookup`` metadata + runway /
frequency summary → ``render`` preview (SVG in an <img>) → ``import`` into
the shared convert pipeline (Page & pen, Label, Display, stats, vpype,
download).

The convert sections are shared Jinja partials with ``/penplot`` (single
source under ``backend/penplot/templates/partials/``) so the pages cannot
drift. The page itself is unthrottled static markup; every expensive call
it makes is already rate-limited.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader

from backend.penplot.backgrounds import BACKGROUNDS, LINE_COLORS
from backend.penplot.config import PAGE_SIZES_MM
from backend.penplot.labels import LABEL_FONTS
from backend.penplot.schemas import ConvertParams

ui_router = APIRouter(tags=["airports-ui"])

_HERE_TEMPLATES = os.path.join(os.path.dirname(__file__), "templates")
_PENPLOT_TEMPLATES = os.path.join(
    os.path.dirname(__file__), "..", "penplot", "templates"
)

_templates = Jinja2Templates(directory=_HERE_TEMPLATES)
# Shared partials live with /penplot — ChoiceLoader keeps one source of
# truth while airports.html lives in this module.
_templates.env.loader = ChoiceLoader(
    [
        FileSystemLoader(_HERE_TEMPLATES),
        FileSystemLoader(_PENPLOT_TEMPLATES),
    ]
)


@ui_router.get("/airports", response_class=HTMLResponse)
async def airports_ui(request: Request) -> HTMLResponse:
    """Render the airport-diagram page with code-accurate defaults."""
    defaults = ConvertParams().model_dump(mode="json")
    return _templates.TemplateResponse(
        request,
        "airports.html",
        {
            "defaults": defaults,
            "methods": ["contour", "centerline", "hatch", "flow"],
            "page_sizes": sorted(PAGE_SIZES_MM),
            "orientations": ["portrait", "landscape"],
            "fonts": list(LABEL_FONTS),
            "line_colors": list(LINE_COLORS),
            "backgrounds": [
                {"id": bid, "label": info["label"]}
                for bid, info in BACKGROUNDS.items()
            ],
        },
    )
