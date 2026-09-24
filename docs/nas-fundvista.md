# fund-vista on NAS — primary pen-plot backend

Status: LIVE since 2026-09-19. The shop calls this backend same-origin
(`/v1/*`); VPS nginx routes to the NAS primary with FastAPI Cloud fallback.

## Topology

```
browser ──► https://penplot.linuxadm.hu/v1/* ──► nginx ──┬──► 127.0.0.1:8100 (reverse tunnel → NAS, container fundvista, 4 workers)
                                                         └──► @fundvista_cloud → https://fund-vista.fastapicloud.dev (fallback, 502/504 only)
```

- NAS: `root@192.168.0.240` (hostname `NAS`, LAN DHCP); source `/opt/fund-vista`
  (`github.com:KissPeter/fund-vista.git`, working tree == `main`).
- Image: `fundvista:nas` via `-f Dockerfile.nas` (port 8100, 4 uvicorn workers, direct
  CMD array — NOT repo-root `Dockerfile` (`sh -c`/`${PORT}`, FastAPI Cloud). `Dockerfile.nas`
  and `.env` (`chmod 600`) are NAS-local, never committed.
- Env keys: `PORT=8100`, `WORKERS=4`, `REDIS_CLOUD_URL=redis://fundvista-redis:6379/0`,
  `PENPLOT_PUBLIC_BASE_URL=https://penplot.linuxadm.hu` (absolute `svg_url`s stay same-
  origin), `PENPIXEL_HMAC_SECRET` (== cloud, HMAC verify cross-backend),
  `PENPLOT_TRUST_FORWARDED_FOR=1`, `PENPLOT_DATA_DIR=/data`.
- Nginx: `/etc/nginx/sites-enabled/penplot.linuxadm.hu`, `location /v1/` (5 s connect,
  310 s read/send — renders/imports/converts run up to ~300 s) + `@fundvista_cloud`.
  `X-Penplot-Upstream: nas|cloud` identifies the serving backend. Backups:
  `/root/penplot.linuxadm.hu.bak-20260919`, `/root/authorized_keys.bak-oc-tunnel-20260919`.
- Container `fundvista`: `--network fundvista-net`, `-p 127.0.0.1:8100:8100`,
  `--cpus=3.0 --memory=4g --restart unless-stopped --env-file /opt/fund-vista/.env`.
- Redis `fundvista-redis` (`redis:7`, volume `fundvista-redis-data:/data`).
- Tunnel: `fundvista-tunnel.service` (NAS systemd, autossh
  `-R 127.0.0.1:8100:127.0.0.1:8100 oc-tunnel@linuxadm.hu`) forwards NAS `:8100` to VPS.
- Shop: `src/lib/penplot.api.ts` (same-origin `/v1/*`, `kp_*` mapping),
  `src/lib/v1proxy.ts` (dev/e2e), `PENPLOT_API_BASE=https://penplot.linuxadm.hu`.

## Pre-deploy gates (all mandatory)

1. Full backend suite green: `./runtests.sh` (docker, `fundvista-dev`).
2. Changes committed on `main` and pushed (`git push origin main`).
3. NAS checkout matches: `git log --oneline -3` on NAS vs local.

## NAS rebuild / update

```sh
ssh -l root 192.168.0.240
cd /opt/fund-vista && git pull origin main && git log --oneline -1
docker build -f Dockerfile.nas -t fundvista:nas .
docker tag fundvista:nas fundvista:nas-prev   # rollback image
docker stop fundvista && docker rm fundvista
docker run -d --name fundvista --network fundvista-net \
  --restart unless-stopped -p 127.0.0.1:8100:8100 \
  --cpus=3.0 --memory=4g --env-file /opt/fund-vista/.env fundvista:nas
sleep 30 && curl -s http://127.0.0.1:8100/healthz
```

## Post-deploy verification

1. `docker ps --format '{{.Names}} {{.Status}}'` — `fundvista` Up, `fundvista-redis`
   Up, no restart loop (4 workers import numpy/cv2: ~30 s to healthy).
2. `docker logs --tail 20 fundvista` — startup line, no traceback.
3. `systemctl is-active fundvista-tunnel.service` — `active`.
4. Public check: `curl -sD - https://penplot.linuxadm.hu/v1/health` →
   `x-penplot-upstream: nas`.

## Failover drill / rollback

```sh
# VPS as root: public health must flip to cloud while NAS app is stopped
ssh -l root 192.168.0.240 'docker stop fundvista'
curl -sD - https://penplot.linuxadm.hu/v1/health -o /dev/null | grep -i x-penplot-upstream
ssh -l root 192.168.0.240 'docker start fundvista'
```

- Backend rollback: `docker stop fundvista && docker rm fundvista && docker tag
  fundvista:nas-prev fundvista:nas` + re-run the same `docker run`; or revert nginx
  to the backup vhost (`nginx -t && systemctl reload nginx`).

## Gotchas

- **No `/data` volume on the app container**: recreating it wipes stored
  images/results (Redis cache survives). Acceptable for anonymous previews;
  retained (purchased) designs re-upload via the shop bridge.
- Image bytes are local per backend (content-addressed sha256): a design uploaded to
  NAS converts only on NAS. Per-request fallback is safe — same bytes re-upload to the
  cloud reproduce the same `image_id`, and the upload page self-heals one silent
  re-upload on convert-404. Token verify is pure HMAC + shared secret, so it works
  cross-backend.
- Rate limiting is per client IP via `X-Forwarded-For` (nginx sets it; backend
  `PENPLOT_TRUST_FORWARDED_FOR=1`). Backend default 100/min/IP shared across CPU endpoints.
- NAS reboot (seen 2026-09-19, everything self-healed): containers restart via
  `unless-stopped`, tunnels via systemd. Health-check before declaring victory.
- NAS LAN IP is DHCP (`192.168.0.240` last seen): if SSH fails, re-probe
  (`nc -z -G 2 <ip> 22`). The stale `81.183.232.91:34022` endpoint is dead — do not use it.
- `svg_url`s are absolute: NAS stamps `https://penplot.linuxadm.hu/v1/...`, the cloud
  stamps its own host. The shop download allow-list accepts both.
- Never include secrets in docs or commit messages; `.env` + `Dockerfile.nas` are
  gitignored and `git pull`/`git status` must stay clean of them.

---

Mirrors the operator runbook in `.opencode/skills/deploy-nas/SKILL.md` — keep
both in sync when anything here changes.