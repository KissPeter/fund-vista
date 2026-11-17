"""Overpass-compatible HTTP front for the local extract.

``POST /api/interpreter`` (form field ``data``, like Overpass) answers the
citymap query shape only. Anything the extract cannot answer faithfully -
unknown query shape, unsupported filter, bbox not fully inside the extract
polygon, stale database - gets ``400``: the citymap mirror walk then tries
the next (public) mirror without parking this one.

Run: ``python -m backend.osm_local.server --db /path/osm-hu.sqlite``
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Form
from fastapi.responses import JSONResponse, PlainTextResponse

from backend.osm_local import ql
from backend.osm_local.store import Store

log = logging.getLogger("backend.osm_local")


def create_app(db_path: Path) -> FastAPI:
    store = Store(db_path)
    app = FastAPI(title="fund-vista osm-local", docs_url=None, redoc_url=None)

    @app.get("/health")
    def health() -> dict:
        return store.health()

    @app.post("/api/interpreter")
    def interpreter(data: str = Form(...)):
        if not store.fresh:
            return PlainTextResponse("database is stale, rebuild", status_code=400)
        try:
            members = ql.parse(data)
        except ql.QueryError as exc:
            return PlainTextResponse(f"unsupported query: {exc}", status_code=400)
        for m in members:
            if not store.supports(m):
                return PlainTextResponse(f"unsupported filter: {m.kind}{m.filter}", status_code=400)
            if not store.covers(m.bbox):
                return PlainTextResponse("bbox outside the local extract", status_code=400)
        return JSONResponse(store.run(members))

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=os.environ.get("OSM_LOCAL_DB"), required=not os.environ.get("OSM_LOCAL_DB"))
    parser.add_argument("--host", default=os.environ.get("OSM_LOCAL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("OSM_LOCAL_PORT", "8200")))
    args = parser.parse_args()
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(Path(args.db)), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
