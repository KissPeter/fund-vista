"""Runtime-tunable limits for the citymap module (env prefix ``CITYMAP_``).

Mirrors the penplot config style (pydantic-settings, fail fast on invalid
values, explicitly-empty env vars treated as unset).
"""

from __future__ import annotations

from typing import Annotated

from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class CitymapSettings(BaseSettings):
    """Knobs for OSM upstreams and the Redis cache window."""

    model_config = SettingsConfigDict(
        env_prefix="CITYMAP_",
        env_ignore_empty=True,
    )

    # Overpass mirrors tried in order; the first healthy one wins.
    # de + kumi + private.coffee are the three long-lived public instances
    # (kumi is operated by Private.coffee; the canonical endpoint outlived
    # several past outages when one of the other two 504'd).
    # osm.fr is the French OSM chapter mirror (EU) — added as a fourth
    # fallback after a triple-504 outage (see docs/hungary-europe-map-providers.md).
    # nchc (Taiwan) added after a full-EU 504 + osm.fr 403 outage — different
    # continent/operator, useful when EU instances are busy at once.
    overpass_urls: Annotated[list[str], NoDecode] = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
        "https://overpass.openstreetmap.fr/api/interpreter",
        "https://overpass.nchc.org.tw/api/interpreter",
    ]
    # Live-query timeout: public mirrors 504 on heavy queries (e.g.
    # buildings over a large bbox). Keep this short so we fail over to the
    # OSM Main API quickly instead of holding a worker for 2 minutes.
    overpass_timeout_s: float = 30.0
    # Per-mirror retries on retryable failures (HTTP 408/429/5xx + network
    # errors). 400/403/404 fail fast for that mirror (no retry — retrying
    # won't help). Backoff is exponential: backoff_s * 2**attempt + jitter.
    overpass_retries: int = 1
    overpass_retry_backoff_s: float = 1.0
    # OSM Main API v0.6 /map fallback (different infra from Overpass, so it
    # survives Overpass outages). Zero new deps: OSM XML -> Overpass-like
    # elements, reused by split_elements/render unchanged.
    # Limit is 0.25 deg^2 per call — bigger bboxes are chunked (see osm_api).
    # Dense city centers also 400 on node count (~50k), so cells start small
    # (max_deg) and subdivide adaptively down to min_deg on 400s.
    osm_api_url: str = "https://api.openstreetmap.org/api/0.6/map"
    osm_api_timeout_s: float = 30.0
    osm_api_max_deg: float = 0.1
    osm_api_min_deg: float = 0.025
    osm_api_retries: int = 2
    osm_api_retry_backoff_s: float = 0.5
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    nominatim_timeout_s: float = 15.0
    # Fixed-window cache TTL for geocode payloads, raw Overpass payloads and
    # rendered SVGs (hours). Results are deterministically regenerable.
    cache_ttl_hours: int = 24
    # Refuse bboxes wider/taller than this (decimal degrees) — a whole
    # country would time out Overpass and OOM the renderer. ~0.8 deg is a
    # large metro area; districts are much smaller. Checked on the bbox as
    # requested, before grid snapping nudges it outward.
    max_bbox_deg: float = 0.8
    # Nominatim usage policy requires a real User-Agent (and asks for a
    # referer/contact on heavy use). Ours identifies the app.
    user_agent: str = "fund-vista-citymap/1.0 (pen-plot city maps)"
    # Rendered SVGs must be retrievable via GET /v1/citymap/results/{token}:
    # the render response carries only the URL, so an uncached SVG means a
    # broken preview (404). Dense metro renders reach ~10 MB, so the cap
    # must fit them — Redis strings allow up to 512 MB; oversized values
    # are still served once and simply re-fetched next time.
    # Checked against the *stored* (deflated) size, so the cap now bites
    # far less often than it did on raw bytes.
    max_cached_bytes: int = 32 * 1024 * 1024
    # Values at or above this are zlib'd before storage. OSM JSON and SVG
    # both deflate 6-10x, which is what pulled dense-metro payloads back
    # under max_cached_bytes — Budapest with buildings was 38-40 MB raw and
    # was silently never cached.
    # Keep this well below a typical tile (~30 KB of JSON): tiles are the
    # bulk of the key space, and leaving them uncompressed cost ~6x the
    # Redis memory of the single blob they replaced. Deflating 30 KB is
    # well under a millisecond, and tiles are written in one pipeline.
    compress_min_bytes: int = 4 * 1024
    # Raw OSM is cached per (grid tile, layer) so panning and layer toggles
    # reuse what is already held (see citymap/tiles.py). A bbox picks the
    # finest tile level that covers it in at most this many tiles: lower
    # means coarser tiles and less reuse, higher means more Redis keys per
    # render (they are read in one MGET, so the cost is mostly key count).
    tile_max_tiles: int = 64
    # The rendered bbox is snapped outward to this grid before it reaches
    # the renderer and the SVG cache key. Raw viewport floats are unique per
    # pan, so without snapping the SVG cache can only ever hit on an exact
    # repeat. ~0.002 deg is ~220 m — invisible on a city-sized plot.
    render_snap_deg: float = 0.002
    # Raw OSM changes far more slowly than the parameters rendered from it,
    # so tiles outlive SVGs. Keeping tiles a week makes a returning user's
    # second session cheap even after every render has expired.
    tile_cache_ttl_hours: int = 168


settings = CitymapSettings()
