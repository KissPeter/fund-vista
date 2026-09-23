---
name: deploy-nas
description: Deploy the fund-vista backend to the NAS (rebuild fundvista:nas, recreate container, verify). Use when asked to deploy, release, or roll out backend changes to production/NAS.
---

# Deploy Backend to NAS

Primary production backend. The VPS (`penplot.linuxadm.hu`) only proxies;
all `/v1` work runs here.

## Topology (fixed)

- **NAS**: `192.168.0.240` (hostname `NAS`, LAN DHCP — re-probe if dead),
  SSH as `root@` with key auth (BatchMode works from the dev machine).
- **Repo**: `/opt/fund-vista` (`github.com:KissPeter/fund-vista.git`).
  Untracked, never commit: `.env` (secrets), `Dockerfile.nas` (NAS variant).
- **Image**: `fundvista:nas`, built with `-f Dockerfile.nas`
  (port `8100`, 4 workers, direct uvicorn array CMD — NOT `Dockerfile`,
  whose `sh -c`/`${PORT}` form is for FastAPI Cloud).
- **App container** `fundvista`: `--network fundvista-net`,
  `-p 127.0.0.1:8100:8100`, `--cpus=3.0 --memory=4g`,
  `--restart unless-stopped`, `--env-file /opt/fund-vista/.env`.
  Key env (in `.env`): `PORT=8100`, `WORKERS=4`,
  `REDIS_CLOUD_URL=redis://fundvista-redis:6379/0`,
  `PENPLOT_PUBLIC_BASE_URL=https://penplot.linuxadm.hu`,
  `PENPLOT_DATA_DIR=/data`.
- **Redis** `fundvista-redis` (`redis:7`, persistent `fundvista-redis-data:/data`).
- **Tunnel**: `fundvista-tunnel.service` (autossh
  `-R 127.0.0.1:8100:127.0.0.1:8100 oc-tunnel@linuxadm.hu`) forwards NAS
  `:8100` to the VPS, where nginx serves it as
  `https://penplot.linuxadm.hu/v1/`.

## Pre-deploy gates (all mandatory)

1. Full backend suite green:
   `./runtests.sh` (docker `fundvista-dev`; adds the P1–P5 performance
   regression + cache-identity tests from docs/CR-002-convert-performance.md).
2. Changes committed on `main` and pushed (`git push origin main`).
3. Confirm what the NAS checkout has: `git log --oneline -3` on NAS vs local.

## Deploy steps (run over root SSH)

```sh
cd /opt/fund-vista && git pull origin main
docker build -f Dockerfile.nas -t fundvista:nas .
docker tag fundvista:nas fundvista:nas-prev   # rollback image
docker stop fundvista && docker rm fundvista
docker run -d --name fundvista --network fundvista-net \
  --restart unless-stopped -p 127.0.0.1:8100:8100 \
  --cpus=3.0 --memory=4g --env-file /opt/fund-vista/.env fundvista:nas
sleep 8
curl -s http://127.0.0.1:8100/healthz
curl -s http://127.0.0.1:8100/healthz | head -c 200
```

## Post-deploy verification

1. `docker ps --format '{{.Names}} {{.Status}}'` — `fundvista` Up,
   `fundvista-redis` Up, no restart loop.
2. `docker logs --tail 20 fundvista` — startup line, no traceback.
3. `systemctl is-active fundvista-tunnel.service` — `active`.
4. Public check: `curl -s https://penplot.linuxadm.hu/v1/jobs/<id>`
   semantics per `docs/render-cancel-plan.md` (enqueue → poll → done;
   overlapping same-IP jobs auto-supersede).
5. Rollback if broken:
   `docker stop fundvista && docker rm fundvista && docker tag
   fundvista:nas-prev fundvista:nas` + re-run the same `docker run`.

## Cautions

- `/opt/fund-vista/.env` and `Dockerfile.nas` are NAS-local and untracked —
  never `git add` them; `git pull` won't touch them.
- The app container has **no /data volume**: recreating it wipes stored
  images/results (Redis cache survives). Acceptable for anonymous previews;
  retained (purchased) designs re-upload via the shop bridge.
- NAS LAN IP is DHCP (`192.168.0.240` last seen): if SSH fails, re-probe
  (`nc -z -G 2 <ip> 22`, ports 22/2222). The stale
  `81.183.232.91:34022` endpoint is dead — do not use it.
- Never include secrets in docs, skills, or commit messages.
