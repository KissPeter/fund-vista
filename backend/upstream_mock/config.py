"""Typed settings for the upstream mock mode (env prefix ``FUNDVISTA_``).

Same pattern as the other modules: pydantic-settings reads the environment
once, validates it and fails fast — no scattered ``os.environ.get`` in the
code (issue #64).

    FUNDVISTA_UPSTREAM_MODE=mock  → mock mode on (any other value = off)
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class UpstreamMockSettings(BaseSettings):
    """Knobs of the mock upstream mode."""

    model_config = SettingsConfigDict(
        env_prefix="FUNDVISTA_",
        env_ignore_empty=True,
    )

    # "mock" swaps every third-party HTTP call for the in-repo fixtures;
    # empty/anything else = the real upstream.
    upstream_mode: str = ""


settings = UpstreamMockSettings()
