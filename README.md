# Fund Vista

Fund Vista tracks K&H and Erste funds, shows return analysis, and stores investment notes in the browser.

## Stack

- Vite
- React 18
- TypeScript
- Tailwind CSS
- shadcn/ui

## Setup

```sh
npm install
cp .env.example .env
cp backend/.env.example backend/.env
```

## Run locally

Frontend:

```sh
npm run dev
```

Backend proxy:

```sh
npm run dev:api
```

## Scripts

- `npm run dev` - start the frontend
- `npm run dev:api` - start the FastAPI proxy
- `npm run build` - build the frontend
- `npm run lint` - lint the frontend

## Backend

The frontend reads its API base URL from `VITE_BACKEND_BASE_URL`.
By default it points to `https://fund-vista.fastapicloud.dev`.

Redis is optional on the proxy: if it is unavailable, requests still work and caching is skipped.

## Production

```sh
docker build -t fundvista:<tag> .
docker run -d --name fundvista --restart unless-stopped \
  -p 127.0.0.1:8000:8000 --env-file backend/.env fundvista:<tag>
```

Production entrypoint is plain uvicorn with `--workers` (see the Dockerfile
for why not `fastapi run`): `PORT` (default 8000) and `WORKERS` (default 4)
come from env. Copy `backend/.env.example` to `backend/.env` and set at
least `PENPIXEL_HMAC_SECRET` (until set, `POST /v1/tokens` answers 503).
Rendered citymap/airport SVGs must fit `max_cached_bytes` (default 32 MB)
or their `results/{token}` preview URL 404s — the render response carries
only the URL, never the bytes. That ceiling applies to the *stored*
(deflated) size; cached values above `compress_min_bytes` are zlib'd.

### Caching

Raw OSM for citymap is cached per `(grid tile, layer)`, not per
`(bbox, layer set)` — see `backend/citymap/tiles.py`. A pan refetches only
the newly exposed tiles and ticking a layer on reuses the layers already
held. Rendered bboxes are snapped to a fixed grid
(`CITYMAP_RENDER_SNAP_DEG`) so the raw viewport floats a map sends do not
make every request a unique key. Airport Overpass radii are rounded up to
`AIRPORTS_RADIUS_BUCKET_M` for the same reason.

Both modules store a small metadata sidecar next to each rendered SVG, so a
cache hit is two small reads rather than a re-fetch and a re-render.

Redis must be configured to evict (`--maxmemory`, `--maxmemory-policy
allkeys-lru`). Under the default `noeviction` a full instance fails every
cache write silently and the hit rate collapses with nothing visibly broken.
