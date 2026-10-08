"""Runtime-tunable limits for the wordmark module (env prefix ``WORDMARK_``).

Mirrors the airports/citymap config style (pydantic-settings, fail fast on
invalid values, explicitly-empty env vars treated as unset). No upstream
knobs: rendering is local CPU, so this module only owns its cache window.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class WordmarkSettings(BaseSettings):
    """Knobs for the wordmark render cache."""

    model_config = SettingsConfigDict(
        env_prefix="WORDMARK_",
        env_ignore_empty=True,
    )

    # Fixed-window cache TTL for rendered SVGs (hours). Results are
    # deterministically regenerable from the request body.
    cache_ttl_hours: int = 24
    # Rendered SVGs must be retrievable via GET /v1/wordmark/results/{token}:
    # the render response carries only the URL, so an uncached SVG means a
    # broken preview (404). Wordmarks are small (tens of KB); the cap mirrors
    # the sibling modules. Checked against the *stored* (deflated) size.
    max_cached_bytes: int = 32 * 1024 * 1024
    # Values at or above this are zlib'd before storage.
    compress_min_bytes: int = 4 * 1024


settings = WordmarkSettings()
