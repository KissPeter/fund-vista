"""Runtime knobs for the weather module (env prefix ``WEATHER_``).

Mirrors the traffic/airports config style (pydantic-settings, fail fast on
invalid values, explicitly-empty env vars treated as unset). The line budget
constants are plain module constants: they are part of the plot contract, not
deployment tuning.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from pydantic_settings import BaseSettings, SettingsConfigDict

#: ERA5 hourly analysis, 0.25 degree, Zarr v3 on Google Cloud (ARCO, anonymous).
ARCO_ERA5_URL = "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"

#: Earliest date the store is trusted for (plan section 4, weather-sources.md).
EARLIEST_DATE = dt.date(1940, 1, 1)

#: Days of ERA5T latency: the newest selectable day is today minus this.
LATENCY_DAYS = 6

#: The analysis times offered to the user (UTC hours).
ALLOWED_HOURS = (0, 6, 12, 18)

#: Europe region box (lon -35..55, lat 33..71), the default preset.


@dataclass(frozen=True)
class Region:
    name: str
    lon0: float
    lon1: float
    lat0: float
    lat1: float


EUROPE = Region(name="europe", lon0=-35.0, lon1=55.0, lat0=33.0, lat1=71.0)
REGIONS = {EUROPE.name: EUROPE}

#: Native grid spacing of the ARCO store (degrees).
GRID_STEP_DEG = 0.25

#: Pressure levels (hPa) read from the store.
LEVEL_Z_T_HPA = 500
LEVEL_WIND_HPA = 250

#: Contour and extrema tuning (validated on the synthetic fixtures in tests).
MSLP_STEP_HPA = 4.0
Z500_STEP_GPM = 60.0
T500_STEP_C = 4.0
#: Gaussian smoothing radius in grid cells before contouring and H/L search.
SMOOTH_SIGMA_CELLS = 1.0
#: Drop contour lines shorter than this (km): tiny closed rings and fragments.
MIN_CONTOUR_LENGTH_KM = 200.0
#: H/L: minimum separation between two markers of the same kind (km).
MIN_EXTREMA_SEPARATION_KM = 600.0
#: H/L: an H must be at least this high, an L at most this low (hPa).
EXTREMA_H_MIN_HPA = 1015.0
EXTREMA_L_MAX_HPA = 1005.0

#: Line budget (plan section 10): about 135 stream paths and 9 m of stream
#: line on the Europe A4 landscape sheet, at the assumed 40 mm/s pen speed.
STREAM_MAX_PATHS = 135
STREAM_PAPER_BUDGET_MM = 9000.0
PLOT_SPEED_MM_S = 40.0
#: Millimetres of paper per degree of latitude on the Europe A4 landscape
#: layout (176 mm of map height over 38 degrees, rounded down).
EUROPE_MM_PER_DEG = 4.6
#: Kilometres per degree of latitude (spherical approximation).
KM_PER_DEG_LAT = 110.57
#: Path-length budget in kilometres of physical wind path, derived from the
#: paper budget above: 9 m of paper at 4.6 mm per degree.
STREAM_MAX_TOTAL_KM = STREAM_PAPER_BUDGET_MM / EUROPE_MM_PER_DEG * KM_PER_DEG_LAT
#: Longest single streamline (degrees of path), so no path hogs the budget.
STREAM_MAX_LINE_DEG = 15.0
#: Streamline seeding separation in grid cells (Jobard-Lefer style).
STREAM_SEPARATION_CELLS = 2.2
#: Minimum speed (m/s) for a streamline seed. 0 keeps every line.
STREAM_MIN_SPEED_MS = 0.0
#: Area below which a closed contour ring is dropped (km^2).
MIN_CLOSED_AREA_KM2 = 2000.0


class WeatherSettings(BaseSettings):
    """Deployment knobs. Defaults are the production values."""

    model_config = SettingsConfigDict(
        env_prefix="WEATHER_",
        env_ignore_empty=True,
    )

    store_url: str = ARCO_ERA5_URL
    # Redis TTL (hours) for a cached field subset. ERA5 values are final once
    # published, so a week only bounds the cache size.
    cache_ttl_hours: int = 24 * 7
    # Upper bound on one cached field subset (checked by cachelib).
    max_cached_bytes: int = 32 * 1024 * 1024
    compress_min_bytes: int = 4 * 1024


settings = WeatherSettings()
