"""Weather mock mode (FUNDVISTA_UPSTREAM_MODE=mock): synthetic ERA5 reader, the
fail-closed guard on the GCS reader, and render + import end to end over HTTP.

Hermetic: no network and no ARCO store. The end-to-end cases start the real
app in a uvicorn subprocess with the mock switch set in its environment, the
same way CI's schemathesis job does.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import numpy as np
import pytest

from backend import upstream_mock
from backend.tests.helpers import TEST_HMAC_SECRET
from backend.upstream_mock.weather import SyntheticEra5Reader
from backend.weather import availability
from backend.weather import fields as F
from backend.weather.config import EUROPE

REPO_ROOT = str(Path(__file__).resolve().parents[2])
WHEN = dt.datetime(2005, 5, 20, 12)
ALL_FIELDS = frozenset({"mslp_hpa", "z500_gpm", "t500_c", "u250", "v250", "u10", "v10"})
LAYERS = ["coast", "borders", "streamlines", "cities", "isobars", "markers", "z500", "isotherms"]
#: Generous upper bound: the 60-85 s GCS hang this replaces must not come back.
FAST_S = 5.0


@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setenv(upstream_mock.ENV_VAR, "mock")


@pytest.fixture
def mock_off(monkeypatch):
    monkeypatch.delenv(upstream_mock.ENV_VAR, raising=False)


# -- the synthetic reader honours the FieldReader contract --------------------

def test_synthetic_reader_returns_the_store_window_grid_and_units(mock_mode):
    sub = SyntheticEra5Reader().read(WHEN, EUROPE, ALL_FIELDS)
    assert sub.region == EUROPE.name and sub.time == WHEN
    # Same window and spacing as the store reader: 33..71 N and -35..55 E at 0.25 deg.
    assert sub.lat.shape == (153,) and sub.lon.shape == (361,)
    assert sub.lat.dtype == np.float32 and sub.lon.dtype == np.float32
    assert np.all(np.diff(sub.lat) > 0) and np.all(np.diff(sub.lon) > 0)
    assert (sub.lat[0], sub.lat[-1], sub.lon[0], sub.lon[-1]) == (33.0, 71.0, -35.0, 55.0)
    for name in ALL_FIELDS:
        values = sub.field(name)
        assert values.shape == (153, 361) and values.dtype == np.float32
        assert np.all(np.isfinite(values)), name
    # Plot units: hPa, geopotential metres, deg C, m/s.
    assert 980.0 < float(sub.mslp_hpa.min()) < 1000.0
    assert 1015.0 < float(sub.mslp_hpa.max()) < 1040.0
    assert 5300.0 < float(sub.z500_gpm.min()) and float(sub.z500_gpm.max()) < 5900.0
    assert -40.0 < float(sub.t500_c.min()) < float(sub.t500_c.max()) < 10.0
    assert float(sub.u250.max()) > 35.0  # a jet, not calm air


def test_synthetic_reader_leaves_unrequested_fields_unset(mock_mode):
    sub = SyntheticEra5Reader().read(WHEN, EUROPE, F.DEFAULT_FIELDS)
    assert sub.z500_gpm is None and sub.t500_c is None and sub.u10 is None
    with pytest.raises(ValueError, match="was not loaded"):
        sub.field("z500_gpm")


def test_synthetic_reader_is_deterministic_and_moves_with_time(mock_mode):
    reader = SyntheticEra5Reader()
    a = reader.read(WHEN, EUROPE, F.DEFAULT_FIELDS)
    b = reader.read(WHEN, EUROPE, F.DEFAULT_FIELDS)
    later = reader.read(WHEN + dt.timedelta(hours=6), EUROPE, F.DEFAULT_FIELDS)
    assert np.array_equal(a.mslp_hpa, b.mslp_hpa) and np.array_equal(a.u250, b.u250)
    assert not np.array_equal(a.mslp_hpa, later.mslp_hpa)


def test_synthetic_reader_rejects_bad_times_and_fields_like_the_store(mock_mode):
    reader = SyntheticEra5Reader()
    with pytest.raises(ValueError, match="not a whole hour"):
        reader.read(WHEN.replace(minute=30), EUROPE, F.DEFAULT_FIELDS)
    with pytest.raises(ValueError, match="not in the store"):
        reader.read(dt.datetime(1939, 12, 31, 12), EUROPE, F.DEFAULT_FIELDS)
    latest = availability.latest_date() + dt.timedelta(days=1)
    with pytest.raises(ValueError, match="not in the store"):
        reader.read(dt.datetime.combine(latest, dt.time(0)), EUROPE, F.DEFAULT_FIELDS)
    with pytest.raises(ValueError, match="unknown weather fields"):
        reader.read(WHEN, EUROPE, frozenset({"nope"}))
    with pytest.raises(ValueError, match="at least one field"):
        reader.read(WHEN, EUROPE, frozenset())


def test_load_subset_serves_the_synthetic_fields_in_mock_mode(mock_mode):
    sub = asyncio.run(F.load_subset(WHEN + dt.timedelta(hours=18), EUROPE, None,
                                    frozenset({"mslp_hpa", "u250", "v250"})))
    assert sub.mslp_hpa.shape == (153, 361) and sub.u250 is not None and sub.z500_gpm is None


# -- the GCS reader is fail-closed in mock mode, untouched otherwise ----------

def test_mock_mode_never_constructs_the_real_reader(mock_mode, monkeypatch):
    constructed: list[object] = []
    real_init = F.ArcoEra5Reader.__init__

    def counting_init(self, *args, **kwargs):
        constructed.append(self)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(F.ArcoEra5Reader, "__init__", counting_init)
    # Reaching the default reader, a render-style load and the direct constructor
    # must all avoid the GCS reader; the direct call must fail closed.
    assert isinstance(F.default_reader(), SyntheticEra5Reader)
    asyncio.run(F.load_subset(WHEN + dt.timedelta(hours=30), EUROPE, None, F.DEFAULT_FIELDS))
    assert constructed == []
    with pytest.raises(upstream_mock.UpstreamNotMocked, match="not constructible"):
        F.ArcoEra5Reader(url="gs://never-used")
    assert len(constructed) == 1  # the guard is inside __init__: only the attempt, no store opened


def test_mock_off_still_uses_the_arco_reader(mock_off):
    reader = F.default_reader()
    assert isinstance(reader, F.ArcoEra5Reader)
    assert not isinstance(reader, SyntheticEra5Reader)
    # Construction is lazy: building the reader opens nothing and needs no network.
    assert F.ArcoEra5Reader(url="gs://unused")._group is None


# -- render and import end to end in a mock-mode app over HTTP ----------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def mock_app(tmp_path_factory):
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update({
        upstream_mock.ENV_VAR: "mock",
        "PENPLOT_DATA_DIR": str(tmp_path_factory.mktemp("weather-mock-data")),
        "LOG_LEVEL": "WARNING",
        "REDIS_CLOUD_URL": "redis://127.0.0.1:1/0",
        "PENPLOT_RATE_LIMIT": "100000",
        "PENPIXEL_HMAC_SECRET": TEST_HMAC_SECRET,
        "PYTHONPATH": REPO_ROOT + os.pathsep + env.get("PYTHONPATH", ""),
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 60
    try:
        while True:
            if proc.poll() is not None:
                raise RuntimeError("mock-mode app exited: " + proc.stderr.read().decode("utf-8", "replace"))
            try:
                if httpx.get(f"{base}/healthz", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("mock-mode app never became healthy")
            time.sleep(0.2)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _body(**extra) -> dict:
    return {"date": "2005-05-20", "time": 12, "layers": LAYERS, "cities": ["Budapest", "Vienna"], **extra}


def test_render_end_to_end_is_fast_valid_svg_with_the_layer_groups(mock_app):
    t0 = time.monotonic()
    r = httpx.post(f"{mock_app}/v1/weather/render", json=_body(), timeout=30)
    elapsed = time.monotonic() - t0
    assert r.status_code == 200, r.text
    assert elapsed < FAST_S, f"render took {elapsed:.1f} s under the mock"

    doc = r.json()
    assert doc["cache_hit"] is False and doc["attribution"]
    svg = httpx.get(f"{mock_app}{urlsplit(doc['svg_url']).path}", timeout=30)
    assert svg.status_code == 200 and svg.headers["content-type"].startswith("image/svg+xml")
    root = ET.fromstring(svg.text)
    assert root.tag.endswith("svg")
    groups = {el.get("data-layer") for el in root.iter() if el.get("data-layer")}
    assert set(LAYERS) <= groups, groups ^ set(LAYERS)
    assert float(doc["stats"]["total"]["mm"]) > 0.0


def test_import_end_to_end_is_fast_and_registers_an_image(mock_app):
    t0 = time.monotonic()
    r = httpx.post(f"{mock_app}/v1/weather/import", json=_body(), timeout=30)
    elapsed = time.monotonic() - t0
    assert r.status_code == 200, r.text
    assert elapsed < FAST_S, f"import took {elapsed:.1f} s under the mock"
    doc = r.json()
    assert doc["image_id"]
    assert set(doc["layers"]) == set(LAYERS)


def test_out_of_window_date_keeps_the_real_422_edge(mock_app):
    r = httpx.post(f"{mock_app}/v1/weather/render", json=_body(date="1900-01-01"), timeout=30)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "out_of_range"
