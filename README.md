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

Production entrypoint is the FastAPI CLI (`fastapi run`, uvicorn engine,
prod defaults): `PORT` (default 8000) and `WORKERS` (default 4) come from
env. Copy `backend/.env.example` to `backend/.env` and set at least
`PENPIXEL_HMAC_SECRET` (until set, `POST /v1/tokens` answers 503).
Rendered citymap/airport SVGs must fit `max_cached_bytes` (default 32 MB)
or their `results/{token}` preview URL 404s — the render response carries
only the URL, never the bytes.
