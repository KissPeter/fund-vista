"""Runtime-tunable limits for the traffic module (env prefix ``TRAFFIC_``).

Mirrors the airports/citymap config style (pydantic-settings, fail fast on
invalid values, explicitly-empty env vars treated as unset).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class TrafficSettings(BaseSettings):
    """Knobs for the adsb.lol ingest, durable slice store and Redis cache."""

    model_config = SettingsConfigDict(
        env_prefix="TRAFFIC_",
        env_ignore_empty=True,
    )

    # Durable slice store backend: "file" (v1, Mac data directory) or "r2"
    # (Phase 7, same keys in a bucket). Any other value fails fast at import.
    store: str = "file"
    # Directory for FileSliceStore. Empty = <PENPLOT_DATA_DIR>/traffic so the
    # Mac /data directory story needs no extra env var (plan §6.5).
    store_dir: str = ""
    # Refuse a new ingest pass below this free-space floor (bytes).
    store_min_free_bytes: int = 5 * 1024 * 1024 * 1024
    # adsb.lol GitHub release lookup timeout (availability check = 1 API call).
    github_timeout_s: float = 20.0
    # Archive streaming: per-part read timeout + HTTP chunk size.
    ingest_timeout_s: float = 60.0
    ingest_read_timeout_s: float = 300.0
    # Worker processes for the archive parse. 0/1 = serial (default: the
    # Mac keeps the old single-threaded pass). >1 = the tar members are
    # parsed in a process pool — the parse is the pass bottleneck
    # (gzip+json+point scans, GIL-bound in-process), the download is not.
    ingest_workers: int = 0
    # Members per pool task. Bigger batches amortise the IPC of shipping
    # member bytes to the workers; smaller ones keep the pipeline flowing.
    ingest_batch_members: int = 200
    # L2 movements (rebuildable from L1) and L3/L4 TTLs live in cache.py;
    # the hour counts below are the only knobs.
    movements_ttl_hours: int = 24 * 30
    # L4 rendered SVGs live a week (plan §6.3); L3 basemap tiles share the
    # 30-day movements window above (slow-changing OSM data).
    svg_ttl_hours: int = 24 * 7
    # Rendered values must fit the 512 MB Redis string cap; checked against
    # the *stored* (deflated) size by cachelib.
    max_cached_bytes: int = 32 * 1024 * 1024
    compress_min_bytes: int = 4 * 1024
    cache_ttl_hours: int = 24 * 30
    # Nominatim-style identification for upstream GETs.
    user_agent: str = "fund-vista-traffic/1.0 (pen-plot airport traffic)"


settings = TrafficSettings()
if settings.store not in ("file", "r2"):
    raise ValueError(f"Invalid TRAFFIC_STORE '{settings.store}': want 'file' or 'r2'.")
