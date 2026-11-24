"""Runtime-tunable limits for the pcb module (env prefix ``PCB_``).

Mirrors the wordmark config: pydantic-settings, fail fast on invalid values.
Only the render cache window is owned here; parsing is local CPU.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class PcbSettings(BaseSettings):
    """Knobs for the Gerber render cache."""

    model_config = SettingsConfigDict(
        env_prefix="PCB_",
        env_ignore_empty=True,
    )

    # Fixed-window TTL for cached renders (hours), 7 days like the airport and
    # traffic caches. The sheet and its uploaded source share this window, so
    # the ink split can always re-plot an order inside it.
    cache_ttl_hours: int = 168
    # Rendered SVGs are small (tens of KB); the cap mirrors the sibling modules.
    max_cached_bytes: int = 32 * 1024 * 1024
    # Values at or above this are zlib'd before storage.
    compress_min_bytes: int = 4 * 1024


settings = PcbSettings()
