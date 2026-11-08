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
#: Kilometres per degree of latitude (spherical approximation).
KM_PER_DEG_LAT = 110.57
#: Streamline seeding separation on the sheet (mm), Jobard-Lefer style. The
#: budget thins by growing this, never by truncating (streamlines.streamlines).
STREAM_SEPARATION_MM = 6.0
#: Shortest kept streamline (mm of paper): shorter dashes are dropped.
STREAM_MIN_LENGTH_MM = 15.0
#: Budget retries: the separation grows and the pass repeats, at most this often.
STREAM_MAX_TRIES = 6
#: Minimum speed (m/s) for a streamline seed. 0 keeps every line (the default
#: hero layer); a future jet-only toggle passes a positive value.
STREAM_MIN_SPEED_MS = 0.0
#: Area below which a closed contour ring is dropped (km^2).
MIN_CLOSED_AREA_KM2 = 2000.0

# -- Phase 2: render layers, cities, plot time ------------------------------

#: Every layer id the render can draw; each is one SVG group ``data-layer``.
LAYER_IDS = ("coast", "borders", "streamlines", "isobars", "z500", "isotherms", "markers", "cities")
#: Order retention for a weather design (days, in hours). A weather copy is
#: promoted to the retained TTL at import so a paid order can always be split
#: from the stored bytes: the re-render would not match after ERA5T revisions.
WEATHER_RETAIN_HOURS = 90 * 24
#: Layers drawn when the request names none. The 250 hPa streamlines are the hero.
DEFAULT_LAYERS = ("coast", "borders", "streamlines", "cities")
#: Fields each layer needs; load_subset reads only the union for the request.
LAYER_FIELDS: dict[str, frozenset[str]] = {
    "coast": frozenset(),
    "borders": frozenset(),
    "cities": frozenset(),
    "streamlines": frozenset({"u250", "v250"}),
    "isobars": frozenset({"mslp_hpa"}),
    "markers": frozenset({"mslp_hpa"}),
    "z500": frozenset({"z500_gpm"}),
    "isotherms": frozenset({"t500_c"}),
}
#: Isotherms: every n-th level is drawn heavier (plan section 1).
ISOTHERM_HEAVY_EVERY = 4

#: Sheet sizes in mm (A4 only, plan section 9 decision 1) and the artwork inset.
#: The inset is the artwork's own clearance from the sheet edge; the frame,
#: title and table are lettered by POST /v1/compose-sheet, not here.
SHEET_MM = {"landscape": (297.0, 210.0), "portrait": (210.0, 297.0)}
ARTWORK_INSET_MM = 12.0

#: Plot time: pen speed in mm/s (no lift time, plan section 1 / Phase 0b).
PLOT_SPEED_MM_S = 40.0
#: Render refuses above this total line length (metres): ``weather_too_long``.
#: Proposal; the owner tunes it (plan section 4).
MAX_PLOT_LENGTH_M = 40.0

#: Stroke widths in mm for the artwork groups (plotter pen widths).
STROKE_MM = 0.25
STROKE_HEAVY_MM = 0.5
CITY_DOT_MM = 0.6
#: Lettering height in mm for city, H/L and value labels (stroke font).
LABEL_HEIGHT_MM = 2.2
LABEL_FONT = "futural"
LABEL_PAD_MM = 0.4

#: Credit for the artwork's data (render response). The exact Copernicus wording
#: is PENDING verification against the C3S licence page (docs/weather-sources.md);
#: this is the placeholder from the plan, lettered by compose-sheet, not drawn here.
ATTRIBUTION = (
    "ERA5 reanalysis, ECMWF / Copernicus Climate Change Service, CC BY 4.0, data modified"
)

#: Region cities, label text and (lat, lon). Budapest is the default city.
CITIES: dict[str, tuple[float, float]] = {
    "Budapest": (47.4979, 19.0402),
    "Vienna": (48.2082, 16.3738),
    "Prague": (50.0755, 14.4378),
    "Warsaw": (52.2297, 21.0122),
    "Berlin": (52.52, 13.405),
    "Paris": (48.8566, 2.3522),
    "London": (51.5074, -0.1278),
    "Madrid": (40.4168, -3.7038),
    "Rome": (41.9028, 12.4964),
    "Athens": (37.9838, 23.7275),
    "Moscow": (55.7558, 37.6173),
    "Stockholm": (59.3293, 18.0686),
}
DEFAULT_CITIES = ("Budapest",)


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
