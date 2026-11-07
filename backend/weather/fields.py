"""Field fetch: one ERA5 time step, one region box, through an injectable reader.

The production reader (:class:`ArcoEra5Reader`) opens the ARCO ERA5 Zarr v3
store with zarr and gcsfs directly (no xarray, the Cloud worker has 512 MB)
and reads only the latitude and longitude window it needs. Tests inject a
:class:`FieldReader` and never touch the network.

Units on :class:`FieldSubset` are the plot units: hPa, geopotential metres,
degrees Celsius, metres per second. Longitudes are -180..180 ascending.

Cache: the subset is stored as compressed npz (base64 inside the string
cache) under ``(date, time, region)``, L1 in the plan.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import io
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from backend.weather.config import EUROPE, LEVEL_WIND_HPA, LEVEL_Z_T_HPA, Region
from backend.weather.config import settings as weather_settings
from backend.weather.cache import cache_get, cache_set, weather_cache_key

G0 = 9.80665
FIELD_NAMES = ("mslp_hpa", "z500_gpm", "t500_c", "u250", "v250", "u10", "v10")

#: ARCO variable names and the conversion applied on read.
ARCO_VARS = {
    "mslp_hpa": ("mean_sea_level_pressure", None),
    "z500_gpm": ("geopotential", LEVEL_Z_T_HPA),
    "t500_c": ("temperature", LEVEL_Z_T_HPA),
    "u250": ("u_component_of_wind", LEVEL_WIND_HPA),
    "v250": ("v_component_of_wind", LEVEL_WIND_HPA),
    "u10": ("10m_u_component_of_wind", None),
    "v10": ("10m_v_component_of_wind", None),
}


@dataclass(frozen=True)
class FieldSubset:
    """One time step of the seven fields on a regular lat/lon box."""

    time: dt.datetime
    region: str
    lat: np.ndarray  # (nlat,) ascending, degrees
    lon: np.ndarray  # (nlon,) ascending, -180..180, degrees
    mslp_hpa: np.ndarray  # (nlat, nlon)
    z500_gpm: np.ndarray
    t500_c: np.ndarray
    u250: np.ndarray  # m/s
    v250: np.ndarray
    u10: np.ndarray
    v10: np.ndarray

    def field(self, name: str) -> np.ndarray:
        return getattr(self, name)


class FieldReader(Protocol):
    """Anything that can return one time step for a region box."""

    def read(self, when: dt.datetime, region: Region) -> FieldSubset: ...


# -- pure helpers (tested without a store) ----------------------------------

def wrap180(lon: np.ndarray) -> np.ndarray:
    """Map longitudes to -180..180 (half-open at 180)."""
    return ((np.asarray(lon, dtype=float) + 180.0) % 360.0) - 180.0


def lon_indices(coords_0_360: np.ndarray, lon0: float, lon1: float) -> np.ndarray:
    """Indices of the grid columns inside the box lon0..lon1, west to east.

    ``coords_0_360`` is the store's longitude axis (0..360). The box may cross
    the Greenwich meridian (Europe: -35..55 is 325..360 then 0..55); the result
    is ordered by eastward offset from ``lon0`` so concatenating the columns
    gives an ascending -180..180 strip after :func:`wrap180`.
    """
    coords = np.mod(np.asarray(coords_0_360, dtype=float), 360.0)
    start = np.mod(lon0, 360.0)
    width = np.mod(lon1 - lon0, 360.0)
    offset = np.mod(coords - start, 360.0)
    inside = np.flatnonzero(offset <= width + 1e-9)
    return inside[np.argsort(offset[inside], kind="stable")]


def contiguous_runs(indices: np.ndarray) -> list[slice]:
    """Split an index list into slices of consecutive integers, in order."""
    if indices.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(indices) != 1) + 1
    runs = []
    for chunk in np.split(indices, breaks):
        runs.append(slice(int(chunk[0]), int(chunk[-1]) + 1))
    return runs


def lat_slice(lat_native: np.ndarray, lat0: float, lat1: float) -> tuple[slice, bool]:
    """Contiguous slice covering lat0..lat1 and whether the axis is descending."""
    idx = np.flatnonzero((lat_native >= lat0 - 1e-9) & (lat_native <= lat1 + 1e-9))
    if idx.size == 0:
        raise ValueError(f"no grid rows in latitude {lat0}..{lat1}")
    return slice(int(idx[0]), int(idx[-1]) + 1), bool(lat_native[0] > lat_native[-1])


# -- production reader --------------------------------------------------------

class ArcoEra5Reader:
    """Reads one time step from the ARCO ERA5 Zarr v3 store, window only.

    zarr and gcsfs are imported lazily so the module imports without them
    (tests, the availability endpoint).
    """

    def __init__(self, url: str | None = None) -> None:
        self._url = url or weather_settings.store_url
        self._group = None
        self._axes: dict | None = None

    def _open(self):
        if self._group is None:
            import zarr  # lazy: network-only dependency

            self._group = zarr.open_group(
                self._url, mode="r", storage_options={"token": "anon"}, use_consolidated=True
            )
        if self._axes is None:
            g = self._group
            self._axes = {
                "lat": np.asarray(g["latitude"][:], dtype=float),
                "lon": np.asarray(g["longitude"][:], dtype=float),
                "time": np.asarray(g["time"][:]),
                "level": np.asarray(g["level"][:], dtype=float),
            }
        return self._group, self._axes

    def read(self, when: dt.datetime, region: Region) -> FieldSubset:
        group, axes = self._open()
        naive = when.replace(tzinfo=None)
        t_hits = np.flatnonzero(axes["time"] == np.datetime64(naive))
        if t_hits.size == 0:
            raise ValueError(f"time {naive.isoformat()} not in the store")
        t_idx = int(t_hits[0])

        lat_sl, lat_desc = lat_slice(axes["lat"], region.lat0, region.lat1)
        lat_vals = axes["lat"][lat_sl]
        lon_idx = lon_indices(axes["lon"], region.lon0, region.lon1)
        runs = contiguous_runs(lon_idx)
        lon_out = wrap180(axes["lon"][lon_idx])

        out: dict[str, np.ndarray] = {}
        for name, (var, level) in ARCO_VARS.items():
            arr = group[var]
            parts = []
            for run in runs:
                if level is None:
                    block = arr[t_idx, lat_sl, run]
                else:
                    l_idx = int(np.flatnonzero(axes["level"] == level)[0])
                    block = arr[t_idx, l_idx, lat_sl, run]
                parts.append(np.asarray(block, dtype=np.float32))
            values = np.concatenate(parts, axis=1)
            if lat_desc:
                values = values[::-1]
            out[name] = _convert(name, values)

        lat_out = lat_vals[::-1] if lat_desc else lat_vals
        return FieldSubset(time=naive, region=region.name, lat=lat_out.astype(np.float32),
                           lon=lon_out.astype(np.float32), **out)


def _convert(name: str, values: np.ndarray) -> np.ndarray:
    """Store units to plot units."""
    if name == "mslp_hpa":
        return (values / 100.0).astype(np.float32)
    if name == "z500_gpm":
        return (values / G0).astype(np.float32)
    if name == "t500_c":
        return (values - 273.15).astype(np.float32)
    return values.astype(np.float32)


_default_reader: ArcoEra5Reader | None = None


def default_reader() -> ArcoEra5Reader:
    global _default_reader
    if _default_reader is None:
        _default_reader = ArcoEra5Reader()
    return _default_reader


# -- npz cache codec -----------------------------------------------------------

def encode_subset(sub: FieldSubset) -> str:
    buf = io.BytesIO()
    arrays = {name: sub.field(name) for name in FIELD_NAMES}
    np.savez_compressed(buf, lat=sub.lat, lon=sub.lon, **arrays)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def decode_subset(payload: str, when: dt.datetime, region: str) -> FieldSubset:
    with np.load(io.BytesIO(base64.b64decode(payload))) as npz:
        return FieldSubset(time=when, region=region, lat=npz["lat"], lon=npz["lon"],
                           **{name: npz[name] for name in FIELD_NAMES})


def subset_cache_key(when: dt.datetime, region: Region) -> str:
    box = f"{region.lon0}:{region.lon1}:{region.lat0}:{region.lat1}"
    return weather_cache_key("fields", when.replace(tzinfo=None).isoformat(), region.name, box)


async def load_subset(when: dt.datetime, region: Region = EUROPE,
                      reader: FieldReader | None = None) -> FieldSubset:
    """Cached field subset for one time step. Cache hit = no reader call."""
    key = subset_cache_key(when, region)
    cached = await cache_get(key)
    if cached is not None:
        return decode_subset(cached, when.replace(tzinfo=None), region.name)
    src = reader or default_reader()
    sub = await asyncio.to_thread(src.read, when, region)
    await cache_set(key, encode_subset(sub))
    return sub
