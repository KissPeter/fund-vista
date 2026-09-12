"""Central configuration for the pen-plot v1 API.

All values are overridable via environment variables so tests can point the
store at a tmp dir without touching production paths.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Runtime-tunable limits. Frozen so handlers can't mutate them by accident."""

    max_upload_bytes: int = field(
        default_factory=lambda: _env_int("PENPLOT_MAX_UPLOAD_BYTES", 10 * 1024 * 1024)
    )
    image_ttl_hours: int = field(
        default_factory=lambda: _env_int("PENPLOT_IMAGE_TTL_HOURS", 48)
    )
    data_dir: str = field(
        default_factory=lambda: os.getenv(
            "PENPLOT_DATA_DIR",
            os.path.join(os.path.dirname(os.path.dirname(__file__)), ".data"),
        )
    )
    # Per-IP fixed-window throttle for the CPU-exposed POST endpoints
    # (spec §5 rate limit, review C.2.2). Redis INCR + EXPIRE, with an
    # in-process fallback window when Redis is down (single-instance v1).
    rate_limit_requests: int = field(
        default_factory=lambda: _env_int("PENPLOT_RATE_LIMIT", 100)
    )
    rate_limit_window_s: int = field(
        default_factory=lambda: _env_int("PENPLOT_RATE_LIMIT_WINDOW_S", 60)
    )
    rate_limit_whitelist: frozenset[str] = field(
        default_factory=lambda: frozenset(
            ip.strip()
            for ip in os.getenv("PENPLOT_RATE_LIMIT_WHITELIST", "").split(",")
            if ip.strip()
        )
    )
    max_image_dim_px: int = field(
        default_factory=lambda: _env_int("PENPLOT_MAX_IMAGE_DIM_PX", 3000)
    )
    low_res_dpi_threshold: float = field(
        default_factory=lambda: _env_float("PENPLOT_LOW_RES_DPI", 100.0)
    )
    # A4 width in mm — the reference for the DPI pre-check (spec §4.1).
    a4_width_mm: float = 210.0

    # Mirrors `read --quantization` in the vpype chain (spec §3.3): every
    # coordinate is snapped to this grid (mm) right after layout, so the
    # vpype_command string in convert responses describes what really ran.
    quantization_mm: float = 0.02

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
