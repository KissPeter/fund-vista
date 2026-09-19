"""Runtime-tunable limits for the airports module (env prefix ``AIRPORTS_``).

Mirrors the citymap config style (pydantic-settings, fail fast on invalid
values, explicitly-empty env vars treated as unset). Independent of
citymap/penplot settings — this module owns its knobs.
"""

from __future__ import annotations

from typing import Annotated

from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class AirportsSettings(BaseSettings):
    """Knobs for OurAirports + Overpass upstreams and the Redis cache window."""

    model_config = SettingsConfigDict(
        env_prefix="AIRPORTS_",
        env_ignore_empty=True,
    )

    # OurAirports CSVs (CC0). Single source (GitHub raw); cached for a day.
    ourairports_base_url: str = (
        "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main"
    )
    ourairports_timeout_s: float = 20.0
    # OSM ground polygons around the airport center (meters). 3000 covers
    # taxiways/aprons/terminals for most fields without timing out Overpass.
    around_radius_m: float = 3000.0
    # Overpass mirrors tried in order; the first healthy one wins (same set
    # as citymap — shared public infra, but configured independently here so
    # this module never imports citymap config).
    overpass_urls: Annotated[list[str], NoDecode] = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
        "https://overpass.openstreetmap.fr/api/interpreter",
        "https://overpass.nchc.org.tw/api/interpreter",
    ]
    overpass_timeout_s: float = 30.0
    overpass_retries: int = 1
    overpass_retry_backoff_s: float = 1.0
    # Fixed-window cache TTL for OurAirports CSVs, raw Overpass payloads and
    # rendered SVGs (hours). Results are deterministically regenerable.
    cache_ttl_hours: int = 24
    # Rendered SVGs must be retrievable via GET /v1/airports/results/{token}:
    # the render response carries only the URL, so an uncached SVG means a
    # broken preview (404). Large-airport renders reach ~10 MB, so the cap
    # must fit them — Redis strings allow up to 512 MB; oversized values
    # are still served once and simply re-fetched next time.
    # Checked against the *stored* (deflated) size.
    max_cached_bytes: int = 32 * 1024 * 1024
    # Values at or above this are zlib'd before storage. The OurAirports
    # CSVs and the Overpass payloads both deflate several-fold (airports.csv
    # alone is ~12 MB of highly repetitive text).
    compress_min_bytes: int = 4 * 1024
    # The Overpass radius is rounded *up* to a multiple of this before it
    # reaches the cache key. radius_m is a continuous UI slider, so every
    # tick used to be its own Overpass round-trip; _effective_radius_m
    # already over-fetches by a 1 km margin, so rounding up never drops
    # geometry — it only stops near-identical radii fragmenting the cache.
    radius_bucket_m: float = 500.0
    # Nominatim-style identification for upstream POSTs.
    user_agent: str = "fund-vista-airports/1.0 (pen-plot airport diagrams)"


settings = AirportsSettings()
