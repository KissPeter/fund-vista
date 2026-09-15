"""Central configuration for the pen-plot v1 API, built by pydantic-settings.

All values are overridable via `PENPLOT_*` environment variables so tests can
point the store at a tmp dir without touching production paths. Typed and
validated at import time: an invalid env value now FAILS FAST instead of being
silently swallowed by a try/except default (the old `_env_int`/_`_env_float`
behaviour). An explicitly-empty env var is treated as unset.
"""

from __future__ import annotations

import os
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime-tunable limits. Frozen so handlers can't mutate them by accident.

    Model fields map 1:1 to ``PENPLOT_<FIELD>`` env vars (case-insensitive),
    e.g. ``max_upload_bytes`` <- ``PENPLOT_MAX_UPLOAD_BYTES``.
    """

    model_config = SettingsConfigDict(
        env_prefix="PENPLOT_",
        env_ignore_empty=True,
        frozen=True,
    )

    max_upload_bytes: int = 25 * 1024 * 1024
    image_ttl_hours: int = 48
    data_dir: str = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), ".data"
    )
    # Per-IP fixed-window throttle for the CPU-exposed POST endpoints
    # (spec §5 rate limit, review C.2.2). Redis SET NX EX + INCR, with an
    # in-process fallback window when Redis is down (single-instance v1).
    # The bucket is SHARED across all four /v1 endpoints (review D.1.5).
    # Aliases pin the legacy env names (field-by-name would read
    # PENPLOT_RATE_LIMIT_REQUESTS).
    rate_limit_requests: int = Field(
        default=100, validation_alias="PENPLOT_RATE_LIMIT"
    )
    rate_limit_window_s: int = 60
    # NoDecode: source-side JSON decode is skipped so the raw "a, b, c" string
    # reaches the split validator below (a frozenset is a "complex" env type).
    rate_limit_whitelist: Annotated[frozenset[str], NoDecode] = frozenset()
    # Review D.1.2: the limiter reads the client IP from X-Forwarded-For with
    # no peer verification. That is correct only behind a proxy/CDN that
    # OVERWRITES the header (it otherwise lets clients rotate IPs past the
    # throttle). v1 is documented to run behind such a proxy; set this False
    # to disable XFF reading on a directly-exposed instance.
    trust_forwarded_for: bool = True
    max_image_dim_px: int = 3000
    # Env name is PENPLOT_LOW_RES_DPI (legacy), hence the alias.
    low_res_dpi_threshold: float = Field(
        default=100.0, validation_alias="PENPLOT_LOW_RES_DPI"
    )
    # A4 width in mm — the reference for the DPI pre-check (spec §4.1).
    a4_width_mm: float = 210.0
    # Absolute public base URL for result links (shop work order §3). When
    # set, svg_url values use this origin instead of the request Host, so
    # links stay absolute + publicly fetchable even behind a proxy that
    # rewrites Host. Empty = derive from the incoming request.
    public_base_url: str = ""
    # Purchased-design retention window (§4, option a): POST
    # /v1/images/{id}/retain promotes an image to this TTL (default 90 d).
    # Anonymous previews keep image_ttl_hours.
    retained_ttl_hours: int = 90 * 24
    # Signed design tokens for the shop bridge (§2). Env names carry the
    # PENPIXEL_ prefix (shared with Woo), hence the explicit aliases —
    # field-by-name would read PENPLOT_HMAC_SECRET instead. Empty secret =
    # signing disabled; POST /v1/tokens answers 503 until one is set.
    hmac_secret: str = Field(default="", validation_alias="PENPIXEL_HMAC_SECRET")
    # Previous secret, accepted during the 24 h rotation dual-accept window.
    hmac_previous_secret: str = Field(
        default="", validation_alias="PENPIXEL_HMAC_SECRET_PREVIOUS"
    )
    # Token lifetime in hours (unix exp = now + ttl). Matches the Woo
    # attach bridge's 24 h expectation.
    token_ttl_hours: int = 24
    # Mirrors `read --quantization` in the vpype chain (spec §3.3): every
    # coordinate is snapped to this grid (mm) right after layout, so the
    # vpype_command string in convert responses describes what really ran.
    quantization_mm: float = 0.02

    @field_validator("rate_limit_whitelist", mode="before")
    @classmethod
    def _split_whitelist(cls, value: object) -> object:
        # Env arrives as "ip1, ip2, ip3" — split to a list so pydantic can build
        # the frozenset. A plain str would otherwise be iterated char-by-char.
        if isinstance(value, str):
            return [ip.strip() for ip in value.split(",") if ip.strip()]
        return value

    @property
    def images_dir(self) -> str:
        return os.path.join(self.data_dir, "images")

    @property
    def results_dir(self) -> str:
        return os.path.join(self.data_dir, "results")


ALLOWED_RASTER_EXTS = frozenset({"png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"})
ALLOWED_EXTS = ALLOWED_RASTER_EXTS | frozenset({"svg"})

# Conservative page table (mm). portrait=True gives (w, h).
PAGE_SIZES_MM = {
    "A5": (148.0, 210.0),
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "LETTER": (215.9, 279.4),
}