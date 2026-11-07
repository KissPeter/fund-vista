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
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from backend.weather.config import EUROPE, LEVEL_WIND_HPA, LEVEL_Z_T_HPA, Region
from backend.weather.config import settings as weather_settings
from backend.weather.cache import cache_get, cache_set, weather_cache_key

G0 = 9.80665

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


#: Fields the default product needs: streamlines (250 hPa wind) and isobars.
#: Z500 and T500 are optional layers and are read only when asked for.
DEFAULT_FIELDS: frozenset[str] = frozenset({"mslp_hpa", "u250", "v250"})

#: Upper bound on concurrent chunk reads. A 4D chunk is about 150 MB decoded,
#: so two at a time keeps the peak under about 300 MB on the 512 MB worker.
MAX_CONCURRENT_READS = 2


def check_fields(fields) -> frozenset[str]:
    """Validate a requested field set against the known ARCO variables."""
    wanted = frozenset(fields)
    unknown = wanted - set(ARCO_VARS)
    if unknown:
        raise ValueError(f"unknown weather fields: {sorted(unknown)}")
    if not wanted:
        raise ValueError("at least one field must be requested")
    return wanted


#: Store variable names (the keys the zarr group exposes).
STORE_VAR_NAMES = frozenset(var for var, _level in ARCO_VARS.values())


@dataclass(frozen=True)
class FieldSubset:
    """One time step on a regular lat/lon box. Only the requested fields are set.

    Unrequested fields stay ``None``; asking for one raises a ValueError that
    names the field, so a caller cannot silently draw an empty layer.
    """

    time: dt.datetime
    region: str
    lat: np.ndarray  # (nlat,) ascending, degrees
    lon: np.ndarray  # (nlon,) ascending, -180..180, degrees
    mslp_hpa: np.ndarray | None = None  # (nlat, nlon)
    z500_gpm: np.ndarray | None = None
    t500_c: np.ndarray | None = None
    u250: np.ndarray | None = None  # m/s
    v250: np.ndarray | None = None
    u10: np.ndarray | None = None
    v10: np.ndarray | None = None

    def field(self, name: str) -> np.ndarray:
        if name not in ARCO_VARS:
            raise ValueError(f"unknown weather field {name!r}")
        value = getattr(self, name)
        if value is None:
            raise ValueError(f"field {name!r} was not loaded; request it in fields")
        return value


class FieldReader(Protocol):
    """Anything that can return one time step for a region box.

    ``fields`` names the arrays to fetch; the rest of the subset stays None.
    """

    def read(self, when: dt.datetime, region: Region,
             fields: frozenset[str] = DEFAULT_FIELDS) -> FieldSubset: ...


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

#: The ARCO store counts whole hours since this instant (proleptic Gregorian).
STORE_EPOCH = dt.datetime(1900, 1, 1)


def time_index(when: dt.datetime, length: int, time_at) -> int:
    """Index of ``when`` (naive UTC) on the store's hourly axis.

    Computes the index from the hour count and checks it against the axis,
    so the time coordinate is never read in full. ``time_at(i)`` returns the
    stored hour value at index ``i``.
    """
    if when.minute or when.second or when.microsecond:
        raise ValueError(f"time {when.isoformat()} is not a whole hour")
    hours = int((when - STORE_EPOCH).total_seconds() // 3600)
    if not 0 <= hours < length or time_at(hours) != hours:
        raise ValueError(f"time {when.isoformat()} not in the store")
    return hours


class ArcoEra5Reader:
    """Reads one time step from the ARCO ERA5 Zarr v3 store, window only.

    zarr and gcsfs are imported lazily so the module imports without them
    (tests, the availability endpoint).
    """

    def __init__(self, url: str | None = None) -> None:
        self._url = url or weather_settings.store_url
        self._group = None
        self._axes: dict | None = None
        self._open_s = 0.0
        #: Seconds of the last read: open (group and axes) and chunk reads.
        self.last_timing: dict[str, float] = {}

    def _open(self):
        t0 = time.perf_counter()
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
                # Store time is int64 hours since STORE_EPOCH, not datetime64.
                "time_arr": g["time"],
                "level": np.asarray(g["level"][:], dtype=float),
            }
        self._open_s = time.perf_counter() - t0
        return self._group, self._axes

    def read(self, when: dt.datetime, region: Region,
             fields: frozenset[str] = DEFAULT_FIELDS) -> FieldSubset:
        wanted = check_fields(fields)
        group, axes = self._open()
        naive = when.replace(tzinfo=None)
        time_arr = axes["time_arr"]
        t_idx = time_index(naive, int(time_arr.shape[0]), lambda i: int(time_arr[i]))

        lat_sl, lat_desc = lat_slice(axes["lat"], region.lat0, region.lat1)
        lat_vals = axes["lat"][lat_sl]
        lon_idx = lon_indices(axes["lon"], region.lon0, region.lon1)
        runs = contiguous_runs(lon_idx)
        lon_out = wrap180(axes["lon"][lon_idx])

        def one(item):
            name, (var, level) = item
            arr = group[var]
            l_idx = None if level is None else int(np.flatnonzero(axes["level"] == level)[0])
            parts = []
            for run in runs:
                if l_idx is None:
                    block = arr[t_idx, lat_sl, run]
                else:
                    block = arr[t_idx, l_idx, lat_sl, run]
                parts.append(np.asarray(block, dtype=np.float32))
            values = np.concatenate(parts, axis=1)
            if lat_desc:
                values = values[::-1]
            return name, _convert(name, values)

        # Only the requested variables are read; at most two at a time.
        t_read = time.perf_counter()
        items = [(name, spec) for name, spec in ARCO_VARS.items() if name in wanted]
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_READS) as pool:
            out = dict(pool.map(one, items))
        self.last_timing = {"open_s": self._open_s, "read_s": time.perf_counter() - t_read}

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


# -- npz cache codec: one entry per (date, time, region, field) ----------------

def encode_field(lat: np.ndarray, lon: np.ndarray, values: np.ndarray) -> str:
    buf = io.BytesIO()
    np.savez_compressed(buf, lat=lat, lon=lon, values=values)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def decode_field(payload: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    with np.load(io.BytesIO(base64.b64decode(payload))) as npz:
        return npz["lat"], npz["lon"], npz["values"]


def field_cache_key(when: dt.datetime, region: Region, name: str) -> str:
    box = f"{region.lon0}:{region.lon1}:{region.lat0}:{region.lat1}"
    return weather_cache_key("field", when.replace(tzinfo=None).isoformat(), region.name, box, name)


async def load_subset(when: dt.datetime, region: Region = EUROPE,
                      reader: FieldReader | None = None,
                      fields: frozenset[str] = DEFAULT_FIELDS) -> FieldSubset:
    """Field subset for one time step, cached per field.

    Cached fields are read back without touching the store; only the missing
    ones go to the reader, in one call. A full cache hit makes no reader call.
    """
    wanted = check_fields(fields)
    names = sorted(wanted)
    keys = {name: field_cache_key(when, region, name) for name in names}
    payloads = await asyncio.gather(*(cache_get(keys[n]) for n in names))

    arrays: dict[str, np.ndarray] = {}
    grid: tuple[np.ndarray, np.ndarray] | None = None
    missing: list[str] = []
    for name, payload in zip(names, payloads):
        if payload is None:
            missing.append(name)
            continue
        lat, lon, values = decode_field(payload)
        arrays[name] = values
        grid = (lat, lon)

    if missing:
        src = reader or default_reader()
        sub = await asyncio.to_thread(src.read, when, region, frozenset(missing))
        grid = (sub.lat, sub.lon)
        for name in missing:
            values = sub.field(name)
            arrays[name] = values
            await cache_set(keys[name], encode_field(sub.lat, sub.lon, values))

    assert grid is not None  # wanted is never empty
    return FieldSubset(time=when.replace(tzinfo=None), region=region.name,
                       lat=grid[0], lon=grid[1], **arrays)
