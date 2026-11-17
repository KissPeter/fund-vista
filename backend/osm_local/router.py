"""Overpass-compatible API over the local extract, served by fund-vista itself.

``POST /v1/osm-local/api/interpreter`` takes the same form field (``data``)
and returns the same JSON as a public Overpass mirror, so any Overpass
client can use it. Citymap prefers it by asking the same code in-process
(``service.fetch_local``) before the public mirrors. A query it cannot
answer faithfully is a ``400`` (like a mirror rejecting a query), never a
partial answer. ``GET /v1/osm-local/health`` reports the loaded extract.
"""

from __future__ import annotations

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse, PlainTextResponse

from backend.osm_local.service import answer, get_store

router = APIRouter(prefix="/v1/osm-local", tags=["osm-local"])


@router.get("/health")
def health() -> dict:
    store = get_store()
    if store is None:
        return {"status": "off"}
    return store.health()


@router.post("/api/interpreter")
def interpreter(data: str = Form(...)):
    result = answer(data)
    if result is None:
        return PlainTextResponse(
            "local extract cannot answer this query (off, stale, unsupported or outside coverage)",
            status_code=400,
        )
    return JSONResponse(result)
