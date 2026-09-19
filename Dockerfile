# fund-vista — production image (NAS primary, FastAPI Cloud fallback).
#
# Build from the repo root:
#   docker build -t fundvista:<tag> .
#
# Run (NAS example — port/workers via env, data + redis alongside):
#   docker network create fundvista-net
#   docker run -d --name fundvista-redis --network fundvista-net \
#     -v fundvista-redis-data:/data --restart unless-stopped redis:7
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

# Production server: FastAPI CLI (uvicorn engine, no reload, prod defaults).
# Long renders/imports/converts run up to ~300 s; the convert path offloads
# to a worker thread so /healthz stays responsive. Always behind a proxy
# that sets X-Forwarded-For/Proto (VPS nginx via reverse tunnel).
CMD ["sh", "-c", "fastapi run main.py --host 0.0.0.0 --port ${PORT:-8000} --workers ${WORKERS:-4} --proxy-headers"]
