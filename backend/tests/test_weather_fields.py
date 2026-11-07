"""Field window, longitude wrap, unit conversion and the npz cache, with no network."""

from __future__ import annotations

import asyncio
import datetime as dt

import numpy as np

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
    pass


def _synthetic_reader():
    """ArcoEra5Reader with an in-memory store: 1 degree grid, 2 times, 3 levels."""
    lat = np.arange(90.0, -91.0, -1.0)  # descending, like ARCO
    lon = np.arange(0.0, 360.0, 1.0)
    times = np.array(["2005-05-20T12:00", "2005-05-20T18:00"], dtype="datetime64[h]")
    levels = np.array([250.0, 500.0, 850.0])
    shape_t = (len(times), len(lat), len(lon))
    msl = np.full(shape_t, 101325.0, dtype=np.float32)  # Pa
    z = np.zeros((len(times), len(levels), len(lat), len(lon)), dtype=np.float32)
    z[:, 1] = 9.80665 * 5600.0  # 500 hPa geopotential, m2/s2 -> 5600 gpm
    t = np.full((len(times), len(levels), len(lat), len(lon)), 258.15, dtype=np.float32)
    u = np.zeros_like(z)
    u[:, 0] = 30.0  # 250 hPa u
    group = _FakeGroup({
        "mean_sea_level_pressure": msl, "geopotential": z, "temperature": t,
        "u_component_of_wind": u, "v_component_of_wind": np.zeros_like(z),
        "10m_u_component_of_wind": np.full(shape_t, 3.0, dtype=np.float32),
        "10m_v_component_of_wind": np.full(shape_t, -4.0, dtype=np.float32),
    })
    reader = F.ArcoEra5Reader(url="gs://unused")
    reader._group = group
    reader._axes = {"lat": lat, "lon": lon, "time": times, "level": levels}
    return reader


def test_reader_window_units_and_wrap():
    reader = _synthetic_reader()
    sub = reader.read(dt.datetime(2005, 5, 20, 12), EUROPE)
    assert sub.lat[0] == 33.0 and sub.lat[-1] == 71.0 and np.all(np.diff(sub.lat) > 0)
    assert sub.lon[0] == -35.0 and sub.lon[-1] == 55.0 and np.all(np.diff(sub.lon) > 0)
    assert sub.mslp_hpa.shape == (len(sub.lat), len(sub.lon))
    assert np.allclose(sub.mslp_hpa, 1013.25)
    assert np.allclose(sub.z500_gpm, 5600.0, atol=1e-2)
    assert np.allclose(sub.t500_c, -15.0, atol=1e-3)
    assert np.allclose(sub.u250, 30.0) and np.allclose(sub.u10, 3.0) and np.allclose(sub.v10, -4.0)


def test_reader_missing_time_raises():
    reader = _synthetic_reader()
    try:
        reader.read(dt.datetime(1999, 1, 1, 0), EUROPE)
    except ValueError as exc:
        assert "not in the store" in str(exc)
    else:
        raise AssertionError("expected ValueError")


class _CountingReader:
    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def read(self, when, region):
        self.calls += 1
        return self.inner.read(when, region)


def test_load_subset_caches_and_skips_reader_on_hit():
    counting = _CountingReader(_synthetic_reader())
    when = dt.datetime(2005, 5, 20, 12)
    small = Region(name="unit-small", lon0=0.0, lon1=10.0, lat0=40.0, lat1=50.0)

    first = asyncio.run(F.load_subset(when, small, counting))
    second = asyncio.run(F.load_subset(when, small, counting))
    assert counting.calls == 1
    assert np.array_equal(first.mslp_hpa, second.mslp_hpa)
    assert np.array_equal(first.lon, second.lon)


def test_npz_codec_roundtrip():
    sub = _synthetic_reader().read(dt.datetime(2005, 5, 20, 18), EUROPE)
    back = F.decode_subset(F.encode_subset(sub), sub.time, sub.region)
    for name in F.FIELD_NAMES:
        assert np.array_equal(sub.field(name), back.field(name))
    assert np.array_equal(sub.lat, back.lat) and np.array_equal(sub.lon, back.lon)
    assert LEVEL_WIND_HPA == 250 and LEVEL_Z_T_HPA == 500
