"""Synthetic ERA5 field reader for ``FUNDVISTA_UPSTREAM_MODE=mock``.

The production reader (:class:`backend.weather.fields.ArcoEra5Reader`) streams
about 150 MB per 4D field from the ARCO store on GCS. In mock mode the weather
layer uses :class:`SyntheticEra5Reader` instead, so the real render pipeline
(contours, streamlines, extrema, composition, ink split, import) runs for real
on smooth, deterministic fields in well under a second, with no network.

The reader honours the :class:`backend.weather.fields.FieldReader` contract:

* the same grid as the store window: latitude ascending 33..71, longitude
  ascending -35..55, both at :data:`backend.weather.config.GRID_STEP_DEG`,
  float32, shape ``(nlat, nlon)``;
* the same units as :class:`FieldSubset` (hPa, geopotential metres, degrees
  Celsius, m/s), only the requested fields set, the rest ``None``;
* the same time axis semantics: whole UTC hours since 1900-01-01, checked with
  :func:`backend.weather.fields.time_index`, so an unknown or fractional time
  raises the same ``ValueError`` the store reader raises;
* the same field names and level handling (500 hPa for Z and T, 250 hPa for
  the wind), and :func:`backend.weather.fields.check_fields` for bad requests.

Nothing here reads the environment beyond the mode switch, uses randomness or
the wall clock except through the availability window, or touches the network.
"""

from __future__ import annotations

import datetime as dt

import numpy as np

from backend.weather import availability
from backend.weather.config import EARLIEST_DATE, EUROPE, GRID_STEP_DEG, Region
from backend.weather.fields import (
    DEFAULT_FIELDS,
    STORE_EPOCH,
    FieldSubset,
    check_fields,
    time_index,
)

#: Period (hours) of the slow eastward drift of the pressure systems.
DRIFT_PERIOD_H = 240.0
#: Amplitude (degrees of longitude) of that drift.
DRIFT_DEG = 4.0


def _hours(when: dt.datetime) -> int:
    return int((when - STORE_EPOCH).total_seconds() // 3600)


def _window_hours() -> tuple[int, int]:
    """First and one-past-last store hour of the synthetic window (the trusted window)."""
    start = _hours(dt.datetime.combine(EARLIEST_DATE, dt.time(0)))
    end = _hours(dt.datetime.combine(availability.latest_date() + dt.timedelta(days=1), dt.time(0)))
    return start, end


def _axes(region: Region) -> tuple[np.ndarray, np.ndarray]:
    """(lat, lon) ascending, float32, on the store grid spacing inside the region box."""
    n_lat = round((region.lat1 - region.lat0) / GRID_STEP_DEG) + 1
    n_lon = round((region.lon1 - region.lon0) / GRID_STEP_DEG) + 1
    lat = region.lat0 + GRID_STEP_DEG * np.arange(n_lat)
    lon = region.lon0 + GRID_STEP_DEG * np.arange(n_lon)
    return lat.astype(np.float32), lon.astype(np.float32)


def _bump(lon2, lat2, lon_c, lat_c, sigma):
    return np.exp(-(((lon2 - lon_c) ** 2 + (lat2 - lat_c) ** 2) / (2.0 * sigma**2)))


def synthetic_fields(hours: int, lat: np.ndarray, lon: np.ndarray) -> dict[str, np.ndarray]:
    """Every field on the (lat, lon) grid for one analysis hour, as plot-unit float32 arrays."""
    lon2, lat2 = np.meshgrid(lon.astype(float), lat.astype(float))
    drift = DRIFT_DEG * np.sin(2.0 * np.pi * hours / DRIFT_PERIOD_H)

    # Sea-level pressure: a deep low over the North Sea, a weaker low to the
    # south-west and a high over central Europe, plus a gentle wave.
    mslp = (
        1013.0
        - 25.0 * _bump(lon2, lat2, 10.0 + drift, 52.0, 5.0)
        - 15.0 * _bump(lon2, lat2, -20.0 + drift, 60.0, 5.0)
        + 20.0 * _bump(lon2, lat2, 30.0, 45.0, 5.0)
        + 2.0 * np.sin(np.radians(3.0 * lon2 + 10.0 * drift)) * _bump(lon2, lat2, 20.0, 55.0, 15.0)
    )

    # 250 hPa wind: a westerly jet near 55 N with meanders and a weak meridional part.
    jet_lat = 55.0 + 0.5 * drift
    u250 = (
        10.0
        + 35.0 * np.exp(-(((lat2 - jet_lat) / 7.0) ** 2))
        + 6.0 * np.cos(np.radians(3.0 * lon2 + 20.0 * drift))
    )
    v250 = 8.0 * np.sin(np.radians(2.5 * lon2 - 10.0 * drift)) * np.exp(-(((lat2 - 52.0) / 12.0) ** 2))

    # 500 hPa geopotential height and temperature: a trough over the low, a
    # meridional gradient, a longitudinal wave in temperature.
    z500 = 5560.0 + 0.9 * (lat2 - 50.0) - 120.0 * _bump(lon2, lat2, 10.0 + drift, 52.0, 8.0) + 40.0 * _bump(
        lon2, lat2, -20.0, 62.0, 8.0
    )
    t500 = -18.0 + 0.45 * (lat2 - 33.0) + 2.0 * np.sin(np.radians(3.0 * lon2))

    # 10 m wind: a damped copy of the upper-level flow.
    u10 = 0.5 * u250
    v10 = 0.5 * v250

    out = {
        "mslp_hpa": mslp,
        "z500_gpm": z500,
        "t500_c": t500,
        "u250": u250,
        "v250": v250,
        "u10": u10,
        "v10": v10,
    }
    return {name: np.asarray(values, dtype=np.float32) for name, values in out.items()}


class SyntheticEra5Reader:
    """Mock stand-in for ``ArcoEra5Reader``: same contract, synthetic values, no network."""

    def __init__(self) -> None:
        #: Kept for parity with the real reader; mock reads take no time.
        self.last_timing: dict[str, float] = {}

    def read(self, when: dt.datetime, region: Region = EUROPE,
             fields: frozenset[str] = DEFAULT_FIELDS) -> FieldSubset:
        wanted = check_fields(fields)
        naive = when.replace(tzinfo=None)
        start, end = _window_hours()

        def time_at(i: int) -> int:
            # Stored value equals the index inside the window; anything else
            # fails the same check the store does.
            return i if start <= i < end else -1

        hours = time_index(naive, end, time_at)
        lat, lon = _axes(region)
        values = synthetic_fields(hours, lat, lon)
        picked = {name: values[name] for name in wanted}
        self.last_timing = {"open_s": 0.0, "read_s": 0.0}
        return FieldSubset(time=naive, region=region.name, lat=lat, lon=lon, **picked)
