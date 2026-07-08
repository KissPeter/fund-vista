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
