# Backend Jinja UI pages (2026-09-15)

Three server-rendered pages, one shared convert stack:
- `docs/ui-penplot-image.md` — GET /penplot: file picker + Method/Cleanup/Page&pen/Label/Display + _convert.html JS (upload/convert/render/debounce) + POST /v1/images|/convert backend (pipeline.py methods->optimize chain).
- `docs/ui-citymap.md` — GET /citymap: Place fieldset + MapLibre/OpenFreeMap visible-bbox preview + layer style toggles; load via POST /v1/citymap/import into vector convert branch. Overpass + OSM API fallback, Nominatim geocode.
- `docs/ui-airports.md` — GET /airports: ICAO search/lookup + radius/zoom/minlen + airfield/context layers + taxiway refs; render+import POST pair into vector convert branch. OurAirports CSVs + Overpass around-fetch, runway-aligned blueprint render.

Shared: `backend/penplot/templates/partials/` single source via ChoiceLoader; `backend/main.py` registers v1 routers before catch-all proxy; all expensive POSTs share penplot per-IP rate limiter; uniform {"error":{"code","message"}} envelope.