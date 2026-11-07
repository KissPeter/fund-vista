"""Field window, longitude wrap, unit conversion and the npz cache, with no network."""

from __future__ import annotations

import asyncio
import datetime as dt

import numpy as np
import pytest

from backend.weather import fields as F
from backend.weather.config import EUROPE, LEVEL_WIND_HPA, LEVEL_Z_T_HPA, Region


def test_lon_indices_wrap_europe_box_west_to_east():
    coords = np.arange(0.0, 360.0, 1.0)  # 1 degree store, 0..359
    idx = F.lon_indices(coords, EUROPE.lon0, EUROPE.lon1)
    lon = F.wrap180(coords[idx])
    assert np.all(np.diff(lon) > 0), "wrapped strip must be ascending"
    assert lon[0] == -35.0 and lon[-1] == 55.0
    assert len(idx) == 91  # -35..55 inclusive at 1 degree


def test_lon_indices_no_wrap_box():
    coords = np.arange(0.0, 360.0, 1.0)
    idx = F.lon_indices(coords, 10.0, 20.0)
    assert list(F.wrap180(coords[idx])) == list(np.arange(10.0, 21.0))


def test_contiguous_runs_split_at_gap():
    runs = F.contiguous_runs(np.array([0, 1, 2, 5, 6]))
    assert runs == [slice(0, 3), slice(5, 7)]
    assert F.contiguous_runs(np.array([], dtype=int)) == []


def test_lat_slice_handles_descending_store():
    lat = np.arange(90.0, -91.0, -1.0)
    sl, desc = F.lat_slice(lat, 33.0, 71.0)
    assert desc is True
    assert lat[sl][0] == 71.0 and lat[sl][-1] == 33.0


class _FakeGroup(dict):
    """Records every data-variable access, so tests can count store reads."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.accessed: list[str] = []

    def __getitem__(self, key):
        if key in F.STORE_VAR_NAMES:
            self.accessed.append(key)
        return super().__getitem__(key)


class _ConstVar:
    """Lazy constant array of a given shape: slices cost only the result size,
    so the full 1940-2050 hourly time axis can be mimicked without memory."""

    def __init__(self, shape, value):
        self.shape = shape
        self.value = np.float32(value)

    def __getitem__(self, key):
        return np.array(np.broadcast_to(self.value, self.shape)[key])


N_HOURS = 1323648  # full ARCO time axis length (hours since 1900-01-01)
N_LAT, N_LON = 181, 360


def _synthetic_reader():
    """ArcoEra5Reader over an in-memory store that mimics the real encoding:
    time as int64 hours since 1900-01-01 (value == index), dims
    (time, level, lat, lon), latitude descending 90..-90, longitude 0..359.
    """
    lat = np.arange(90.0, -91.0, -1.0)
    lon = np.arange(0.0, 360.0, 1.0)
    levels = np.array([250.0, 500.0, 850.0])
    t3 = (N_HOURS, N_LAT, N_LON)
    t4 = (N_HOURS, len(levels), N_LAT, N_LON)
    group = _FakeGroup({
        "time": np.arange(N_HOURS, dtype=np.int64),
        "mean_sea_level_pressure": _ConstVar(t3, 101325.0),  # Pa
        "geopotential": _ConstVar(t4, 9.80665 * 5600.0),  # m2/s2 -> 5600 gpm
        "temperature": _ConstVar(t4, 258.15),
        "u_component_of_wind": _ConstVar(t4, 30.0),
        "v_component_of_wind": _ConstVar(t4, 0.0),
        "10m_u_component_of_wind": _ConstVar(t3, 3.0),
        "10m_v_component_of_wind": _ConstVar(t3, -4.0),
    })
    reader = F.ArcoEra5Reader(url="gs://unused")
    reader._group = group
    reader._axes = {"time_arr": group["time"], "lat": lat, "lon": lon, "level": levels}
    return reader


def test_time_index_matches_store_hours():
    # 2005-05-20 12:00 is 923772 whole hours after 1900-01-01 00:00.
    assert F.time_index(dt.datetime(2005, 5, 20, 12), N_HOURS, lambda i: i) == 923772
    assert F.STORE_EPOCH == dt.datetime(1900, 1, 1)


def test_time_index_out_of_range_raises():
    with pytest.raises(ValueError, match="not in the store"):
        F.time_index(dt.datetime(1899, 12, 31, 23), N_HOURS, lambda i: i)
    with pytest.raises(ValueError, match="not in the store"):
        F.time_index(dt.datetime(2051, 1, 1), N_HOURS, lambda i: i)
    with pytest.raises(ValueError, match="whole hour"):
        F.time_index(dt.datetime(2005, 5, 20, 12, 30), N_HOURS, lambda i: i)


def test_reader_reads_real_encoded_time():
    reader = _synthetic_reader()
    sub = reader.read(dt.datetime(2005, 5, 20, 12), EUROPE)
    assert sub.mslp_hpa.shape == (len(sub.lat), len(sub.lon))
    assert np.allclose(sub.mslp_hpa, 1013.25)


def test_reader_window_units_and_wrap():
    reader = _synthetic_reader()
    sub = reader.read(dt.datetime(2005, 5, 20, 12), EUROPE, frozenset(F.ARCO_VARS))
    assert sub.lat[0] == 33.0 and sub.lat[-1] == 71.0 and np.all(np.diff(sub.lat) > 0)
    assert sub.lon[0] == -35.0 and sub.lon[-1] == 55.0 and np.all(np.diff(sub.lon) > 0)
    assert sub.mslp_hpa.shape == (len(sub.lat), len(sub.lon))
    assert np.allclose(sub.mslp_hpa, 1013.25)
    assert np.allclose(sub.z500_gpm, 5600.0, atol=1e-2)
    assert np.allclose(sub.t500_c, -15.0, atol=1e-3)
    assert np.allclose(sub.u250, 30.0) and np.allclose(sub.u10, 3.0) and np.allclose(sub.v10, -4.0)


def test_reader_missing_time_raises():
    reader = _synthetic_reader()
    with pytest.raises(ValueError, match="not in the store"):
        reader.read(dt.datetime(2060, 1, 1, 0), EUROPE)


class _CountingReader:
    def __init__(self, inner):
        self.inner = inner
        self.calls = 0
        self.requested: list[frozenset] = []

    def read(self, when, region, fields=F.DEFAULT_FIELDS):
        self.calls += 1
        self.requested.append(frozenset(fields))
        return self.inner.read(when, region, fields)


def test_load_subset_caches_and_skips_reader_on_hit():
    counting = _CountingReader(_synthetic_reader())
    when = dt.datetime(2005, 5, 20, 12)
    small = Region(name="unit-small", lon0=0.0, lon1=10.0, lat0=40.0, lat1=50.0)

    first = asyncio.run(F.load_subset(when, small, counting))
    second = asyncio.run(F.load_subset(when, small, counting))
    assert counting.calls == 1
    assert np.array_equal(first.mslp_hpa, second.mslp_hpa)
    assert np.array_equal(first.lon, second.lon)


def test_field_codec_roundtrip():
    sub = _synthetic_reader().read(dt.datetime(2005, 5, 20, 18), EUROPE, frozenset(F.ARCO_VARS))
    lat, lon, values = F.decode_field(F.encode_field(sub.lat, sub.lon, sub.mslp_hpa))
    assert np.array_equal(values, sub.mslp_hpa)
    assert np.array_equal(lat, sub.lat) and np.array_equal(lon, sub.lon)
    assert LEVEL_WIND_HPA == 250 and LEVEL_Z_T_HPA == 500


def test_unrequested_field_raises_clear_error():
    sub = _synthetic_reader().read(dt.datetime(2005, 5, 20, 12), EUROPE)
    assert sub.z500_gpm is None
    with pytest.raises(ValueError, match="z500_gpm.*not loaded"):
        sub.field("z500_gpm")


def test_default_fields_touch_only_three_variables():
    reader = _synthetic_reader()
    reader.read(dt.datetime(2005, 5, 20, 12), EUROPE)
    assert sorted(reader._group.accessed) == sorted(
        ["mean_sea_level_pressure", "u_component_of_wind", "v_component_of_wind"])


def test_optional_z500_reads_only_geopotential_and_caches_per_field():
    counting = _CountingReader(_synthetic_reader())
    when = dt.datetime(2007, 3, 3, 0)
    asyncio.run(F.load_subset(when, EUROPE, counting))  # default: 3 fields, 1 call
    assert counting.requested == [F.DEFAULT_FIELDS]
    group = counting.inner._group
    before = len(group.accessed)
    z = asyncio.run(F.load_subset(when, EUROPE, counting, frozenset({"z500_gpm"})))
    assert group.accessed[before:] == ["geopotential"]
    assert z.z500_gpm is not None and z.mslp_hpa is None
    assert np.allclose(z.z500_gpm, 5600.0, atol=1e-2)


def test_cache_hit_makes_no_reader_call():
    counting = _CountingReader(_synthetic_reader())
    when = dt.datetime(2007, 4, 4, 6)
    asyncio.run(F.load_subset(when, EUROPE, counting))
    group = counting.inner._group
    n_access = len(group.accessed)
    again = asyncio.run(F.load_subset(when, EUROPE, counting))
    assert counting.calls == 1
    assert len(group.accessed) == n_access
    assert again.u250 is not None



@pytest.mark.network
def test_live_arco_smoke_2005_05_20_12utc():
    """Live read from the ARCO store. Skipped by default; run with -m network."""
    import time

    reader = F.ArcoEra5Reader()
    t0 = time.time()
    sub = reader.read(dt.datetime(2005, 5, 20, 12), EUROPE)
    print(f"\nLIVE read seconds={time.time() - t0:.1f} timing={reader.last_timing}")
    assert sub.mslp_hpa.shape == (len(sub.lat), len(sub.lon))
    assert np.all(np.diff(sub.lon) > 0) and sub.lon[0] == -35.0 and sub.lon[-1] == 55.0
    assert np.all(np.diff(sub.lat) > 0) and sub.lat[0] == 33.0 and sub.lat[-1] == 71.0
    assert 950.0 <= float(np.nanmin(sub.mslp_hpa)) and float(np.nanmax(sub.mslp_hpa)) <= 1060.0
    print(f"LIVE mslp min={float(np.nanmin(sub.mslp_hpa)):.1f} max={float(np.nanmax(sub.mslp_hpa)):.1f}")
