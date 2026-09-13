"""Server-rendered UI for city maps (Jinja2 template).

One page (``GET /citymap``): place search with candidate picker (same-named
places), a pannable/zoomable MapLibre preview on free OpenFreeMap vector
tiles, layer filters as style toggles, then load-via-visible-bbox through
``POST /v1/citymap/import`` into the shared convert pipeline (Page & pen,
Label, Display, stats, vpype, download).

The convert sections are shared Jinja partials with ``/penplot`` (single
source under ``backend/penplot/templates/partials/``) so the two pages
cannot drift. The page itself is unthrottled static markup; every expensive
call it makes is already rate-limited.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader

from backend.citymap.layers import LAYERS as CITYMAP_LAYERS
from backend.citymap.layers import LAYER_ORDER as CITYMAP_LAYER_ORDER
from backend.penplot.backgrounds import BACKGROUNDS, LINE_COLORS
from backend.penplot.config import PAGE_SIZES_MM
from backend.penplot.labels import LABEL_FONTS
from backend.penplot.schemas import ConvertParams

ui_router = APIRouter(tags=["citymap-ui"])

_HERE_TEMPLATES = os.path.join(os.path.dirname(__file__), "templates")
_PENPLOT_TEMPLATES = os.path.join(
    os.path.dirname(__file__), "..", "penplot", "templates"
)

_templates = Jinja2Templates(directory=_HERE_TEMPLATES)
# Shared partials live with /penplot — ChoiceLoader keeps one source of
# truth while citymap.html lives in this module.
_templates.env.loader = ChoiceLoader(
    [
        FileSystemLoader(_HERE_TEMPLATES),
        FileSystemLoader(_PENPLOT_TEMPLATES),
    ]
)

MAPLIBRE_VERSION = "4.7.1"


@ui_router.get("/citymap", response_class=HTMLResponse)
async def citymap_ui(request: Request) -> HTMLResponse:
    """Render the city-map page with code-accurate defaults."""
    defaults = ConvertParams().model_dump(mode="json")
    return _templates.TemplateResponse(
        request,
        "citymap.html",
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
            "citymap_layers": [
                {"id": lid, "label": CITYMAP_LAYERS[lid]["label"]}
                for lid in CITYMAP_LAYER_ORDER
            ],
            "maplibre_version": MAPLIBRE_VERSION,
        },
    )
