# B-001 — Outbound HTTPS dies on an unreadable certifi bundle

Status: done (guard + healthz surfacing). 2026-10-20.

## Symptom

`GET https://pen-pixel.hu/v1/traffic/availability?icao=LHBP&date=2026-10-17` →

```json
{"error":{"code":"ourairports_unavailable","message":"Airport lookup failed:
[Errno 5] Input/output error: '/usr/local/lib/python3.13/site-packages/certifi/cacert.pem'"}}
```

## Root cause

`httpx` builds its SSL context **eagerly** in the `AsyncClient(...)` /
`Client(...)` constructor — httpx `_config.SSLConfig.load_ssl_context_verify`
reads `certifi.where()` off disk **before any network I/O**. On the NAS
`fundvista` container that file was unreadable at the filesystem level
(`Errno 5` EIO = corrupted image layer on the NAS storage), so **every
outbound HTTPS call failed identically**, no matter the code path:

- OurAirports CSV fetches (`airports/ourairports.py`) — surfaced as 502
  `ourairports_unavailable` on the traffic and airports tab;
- Overpass / Nominatim / OSM-OS basemap fetches (`airports/overpass.py`,
  `citymap/*`);
- the kh.hu/Erste/frankfurter catch-all proxy (`main.py` `http_client`);
- the adsb.lol archive ingest and `penplot/img2plot.py` URL loads.

Warm Redis caches (24 h TTL) masked it: only cache-MISS requests tripped the
constructor. Stable per-process caches (parsed rows) meant no new SSL context
read was constructor. Stable per-process caches (parsed rows) meant no new SSL context
read was triggered while caches were hot — the breakage appeared when the
24 h TTLs rolled over, not because anything in the app changed.

## Fix — `backend/http/tls.py` (fix-once home `backend/http/`)

- `async_client()` / `client()` probe the certifi bundle (a 1-byte read) and,
  when it is unreadable, build the client with `verify=` the first readable
  system CA bundle (`SSL_CERT_FILE`, `/etc/ssl/certs/ca-certificates.crt`,
  `/etc/ssl/cert.pem`, …). TLS stays **verified** — `verify=False` is never
  injected; caller-supplied `verify` (False / SSLContext / path) wins.
- When no readable bundle exists, kwargs are passed untouched so httpx raises
  its original OSError and routers keep mapping to their 502 envelopes — plus
  “ourairports” now wraps it with the remediation hint (“container image needs
  a rebuild”).
- `ca_status()` feeds `/healthz`: `outbound_tls: certifi` (healthy) →
  `fallback:<path> (certifi unreadable)` → `degraded` when even the fallback
  fails.

All production client constructions route through the guard: `main.py`,
`traffic/router.py`, `airports/{ourairports,overpass}.py`,
`citymap/{overpass,geocode,osm_api}.py`, `jobs/_exec_traffic.py`,
`penplot/img2plot.py`.

## NAS remediation (the image layer itself still needs rebuilding)

Code-level fallback keeps the service alive, but the corruption is in the
deployed image layered on NAS storage — once NAS SSH is reachable again:

```sh
docker exec fundvista python -c "import certifi,pathlib; \
  pathlib.Path(certifi.where()).read_bytes()"   # confirm Errno 5
cd /opt/fund-vista && git pull origin main      # pick up the guard
docker tag fundvista:nas fundvista:nas-prev
docker build -f Dockerfile.nas -t fundvista:nas .
docker stop fundvista && docker rm fundvista
docker run -d --name fundvista --network fundvista-net \
  --restart unless-stopped -p 127.0.0.1:8100:8100 \
  --cpus=3.0 --memory=4g --env-file /opt/fund-vista/.env fundvista:nas
curl -s http://127.0.0.1:8100/healthz           # expect outbound_tls: certifi
```

Also worth checking NAS volume health (`docker volume inspect`, SMART) —
`Errno 5` on a cached layer read is a storage-level event; a one-off layer
glitch is possible, a recurring one is a drive/failure warning.

## Tests

`backend/tests/test_tls_cabundle.py` (hermetic): readable certifi →
construction untouched; unreadable → first readable fallback used with
verified TLS; `SSL_CERT_FILE` precedence; caller `verify` wins; no fallback →
original OSError; `# ourairports` wrap; `/healthz` states. Full suite:
612 passed, 1 skipped.
