# fund-vista — production image (NAS primary, FastAPI Cloud fallback).
#
# Build from the repo root:
#   docker build -t fundvista:<tag> .
#
# Run (NAS example — port/workers via env, data + redis alongside):
#   docker network create fundvista-net
#   docker run -d --name fundvista-redis --network fundvista-net \
#     -v fundvista-redis-data:/data --restart unless-stopped redis:7 \
#     redis-server --maxmemory 1gb --maxmemory-policy allkeys-lru
#   # The cache is regenerable, so it must evict rather than refuse writes.
#   # Default redis:7 runs maxmemory 0 / noeviction: once full, every
#   # cache_set fails silently (they are swallowed to a log line) and the
#   # hit rate collapses with nothing obviously broken. Set on the running
#   # container with CONFIG SET, but that is lost on recreate — keep the
#   # flags here.
#   docker run -d --name fundvista --network fundvista-net \
#     --restart unless-stopped -p 127.0.0.1:8100:8100 \
#     --cpus=3.0 --memory=4g --env-file /opt/fund-vista/.env fundvista:nas
#
# Required env (see backend/.env.example): REDIS_CLOUD_URL,
# PENPLOT_PUBLIC_BASE_URL, PENPIXEL_HMAC_SECRET, PENPLOT_DATA_DIR=/data.
# Optional: PORT (default 8000), WORKERS (default 4).

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/fund-vista

# Dependency layer first (rebuild-cache friendly: code changes below
# do not re-run pip).
COPY backend/pyproject.toml ./backend/pyproject.toml
RUN pip install --upgrade pip && pip install ./backend

COPY backend ./backend
COPY main.py ./

# Overridden at run time via PENPLOT_DATA_DIR=/data (named volume).
ENV PENPLOT_DATA_DIR=/data

EXPOSE 8000

# Production server: plain uvicorn with workers. Do NOT use `fastapi run`
# with --workers here: its multiprocess supervisor kills workers on
# requests past ~60 s (long renders die mid-request with the connection
# reset — observed 2026-09-19), while plain uvicorn serves 140 s+ renders
# fine. Same engine, no supervisor in the way.
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WORKERS:-4} --proxy-headers"]
