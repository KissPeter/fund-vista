"""Hybrid airfield source (F-008): routing, filters and failover.

No network. The Overture reader and ``fetch_airport_polygons`` are replaced
with in-memory fakes, so each test pins one behaviour of
``backend.airports.sources.load_airfield``.
"""

from __future__ import annotations

import asyncio
import math
import threading

import httpx
import pytest

from backend.airports import sources
from backend.airports.overpass import (
    AEROWAY_CLASSES,
    AirportOverpassError,
    build_airport_query,
)

LAT, LON, R = 47.0, 19.0, 1000.0


@pytest.fixture(autouse=True)
def _fresh_overture_state():
    """The Overture outage window and STAC cache are module state."""
    sources.reset_overture_state()
    yield
    sources.reset_overture_state()


def _ll(east_m: float, north_m: float) -> tuple[float, float]:
    """Metres east and north of the centre to (lon, lat)."""
    mlon = 111320.0 * math.cos(math.radians(LAT))
    return LON + east_m / mlon, LAT + north_m / 110540.0


def _line_m(*pts: tuple[float, float]) -> list[tuple[float, float]]:
    return [_ll(e, n) for e, n in pts]


def _overture_row(cls: str, pts, ref: str | None = None) -> dict:
    return {
        "class": cls,
        "ref": ref,
        "geometry": {"type": "LineString", "coordinates": [list(p) for p in pts]},
    }


def _way(aeroway: str, pts, ref: str | None = None, way_id: int = 1) -> dict:
    tags = {"aeroway": aeroway}
    if ref is not None:
        tags["ref"] = ref
    return {
        "type": "way",
        "id": way_id,
        "tags": tags,
        "geometry": [{"lon": lon, "lat": lat} for lon, lat in pts],
    }


class _Overture:
    """Stands in for ``read_overture_airfield``; records every call."""

    def __init__(self, rows=(), error: Exception | None = None):
        self.rows = list(rows)
        self.error = error
        self.calls: list[tuple] = []

    def __call__(self, lat, lon, radius_m, release):
        self.calls.append((lat, lon, radius_m, release))
        if self.error is not None:
            raise self.error
        return list(self.rows)


class _Overpass:
    """Stands in for ``fetch_airport_polygons``; routes by query shape."""

    def __init__(self, full=(), reduced=(), fail_full=False, fail_reduced=False):
        self.full = list(full)
        self.reduced = list(reduced)
        self.fail_full = fail_full
        self.fail_reduced = fail_reduced
        self.queries: list[str] = []

    async def __call__(self, query, client=None):
        self.queries.append(query)
        if "parking_position" in query:
            if self.fail_reduced:
                raise AirportOverpassError("reduced query: all mirrors down")
            return list(self.reduced)
        if self.fail_full:
            raise AirportOverpassError("full query: all mirrors down")
        return list(self.full)


def _install(monkeypatch, overture: _Overture, overpass: _Overpass) -> None:
    monkeypatch.setattr(sources.settings, "overture_enabled", True)
    monkeypatch.setattr(sources, "read_overture_airfield", overture)
    monkeypatch.setattr(sources, "fetch_airport_polygons", overpass)


def _load():
    return asyncio.run(sources.load_airfield(LAT, LON, R))


def test_circle_filter_drops_feature_in_bbox_corner(monkeypatch):
    # Nearest point (900, 900) m is 1273 m from the centre: outside the 1000 m
    # circle, but inside the 1000 m half-width bbox. Must be dropped.
    corner = _overture_row("apron", _line_m((900, 900), (950, 950)))
    inside_pts = _line_m((0, 500), (50, 520))
    inside = _overture_row("apron", inside_pts)
    _install(monkeypatch, _Overture([corner, inside]), _Overpass())

    result = _load()

    assert result.geoms["apron"] == [inside_pts]


def test_circle_filter_applies_to_osm_layers_too(monkeypatch):
    far = _way("terminal", [_ll(900, 900), _ll(950, 950)], way_id=7)
    near = _way("terminal", [_ll(100, 100), _ll(120, 120)], way_id=8)
    # Overture must return rows: an empty read is now a fallback (overture_empty).
    overture = _Overture([_overture_row("runway", _line_m((-200, 0), (200, 0)))])
    _install(monkeypatch, overture, _Overpass(reduced=[far, near]))

    result = _load()

    assert len(result.geoms["terminal"]) == 1


def test_runways_under_50_m_are_dropped_other_layers_are_not(monkeypatch):
    short_rwy = _overture_row("runway", _line_m((0, 0), (30, 0)))
    long_rwy = _overture_row("runway", _line_m((-100, 0), (100, 0)))
    short_apron = _overture_row("apron", _line_m((0, 0), (30, 0)))
    _install(
        monkeypatch,
        _Overture([short_rwy, long_rwy, short_apron]),
        _Overpass(),
    )

    result = _load()

    assert len(result.geoms["runway"]) == 1
    assert len(result.geoms["apron"]) == 1
    assert sources.MIN_RUNWAY_M == 50.0


def test_layer_routing_and_sources(monkeypatch):
    overture = _Overture(
        [
            _overture_row("runway", _line_m((-200, 0), (200, 0))),
            _overture_row("taxiway", _line_m((0, -50), (0, 50)), ref="A1"),
            _overture_row("taxilane", _line_m((50, -50), (50, 50))),
            _overture_row("apron", _line_m((100, 100), (150, 150), (100, 150))),
            _overture_row("stopway", _line_m((-250, 0), (-200, 0))),
        ]
    )
    reduced = [
        _way("terminal", [_ll(300, 300), _ll(320, 300)], way_id=1),
        _way("hangar", [_ll(-300, 300), _ll(-280, 300)], way_id=2),
        _way("parking_position", [_ll(400, 0), _ll(410, 0)], way_id=3),
    ]
    overpass = _Overpass(reduced=reduced)
    _install(monkeypatch, overture, overpass)

    result = _load()

    assert set(result.geoms) == set(AEROWAY_CLASSES)
    assert len(result.geoms["runway"]) == 1
    assert len(result.geoms["taxiway"]) == 2  # taxiway + taxilane
    assert len(result.geoms["apron"]) == 1
    assert len(result.geoms["stopways"]) == 1
    assert len(result.geoms["terminal"]) == 1
    assert len(result.geoms["hangar"]) == 1
    assert len(result.geoms["stands"]) == 1
    assert result.sources == {
        "runway": "overture",
        "taxiway": "overture",
        "apron": "overture",
        "stopways": "overture",
        "terminal": "osm",
        "hangar": "osm",
        "stands": "osm",
    }
    assert result.warnings == []
    assert result.overture_release == sources.settings.overture_release
    assert len(overpass.queries) == 1
    assert overpass.queries[0] == sources.build_reduced_airport_query(LAT, LON, R)
    assert "out geom" in overpass.queries[0]
    assert "parking_position|hangar|terminal" in overpass.queries[0]


def test_taxiway_labels_come_from_overture_taxiway_ref(monkeypatch):
    labelled = _line_m((0, -50), (0, 50))
    lane = _line_m((50, -50), (50, 50))
    unnamed = _line_m((-50, -50), (-50, 50))
    overture = _Overture(
        [
            _overture_row("taxiway", labelled, ref="A1"),
            _overture_row("taxilane", lane, ref="B2"),
            _overture_row("taxiway", unnamed, ref=None),
        ]
    )
    _install(monkeypatch, overture, _Overpass())

    result = _load()

    assert result.taxiway_refs == [("A1", labelled)]


def test_overture_failure_falls_back_to_full_overpass(monkeypatch):
    full = [
        _way("runway", _line_m((-200, 0), (200, 0)), way_id=1),
        _way("terminal", [_ll(300, 300), _ll(320, 300)], way_id=2),
        _way("taxiway", _line_m((0, -60), (0, 60)), ref="C3", way_id=3),
    ]
    overpass = _Overpass(full=full)
    _install(monkeypatch, _Overture(error=RuntimeError("S3 unreachable")), overpass)

    result = _load()

    assert result.warnings == ["overture_unavailable"]
    assert result.sources == {cls: "osm" for cls in AEROWAY_CLASSES}
    assert result.overture_release is None
    assert len(result.geoms["terminal"]) == 1
    assert len(result.geoms["runway"]) == 1
    assert [ref for ref, _ in result.taxiway_refs] == ["C3"]
    assert build_airport_query(LAT, LON, R) in overpass.queries


def test_reduced_overpass_failure_keeps_overture_layers(monkeypatch):
    overture = _Overture(
        [_overture_row("runway", _line_m((-200, 0), (200, 0)))]
    )
    _install(monkeypatch, overture, _Overpass(fail_reduced=True))

    result = _load()

    assert result.warnings == ["osm_layers_unavailable"]
    assert result.sources == {
        "runway": "overture",
        "taxiway": "overture",
        "apron": "overture",
        "stopways": "overture",
    }
    assert len(result.geoms["runway"]) == 1
    assert result.geoms["terminal"] == []
    assert result.geoms["hangar"] == []
    assert result.geoms["stands"] == []
    assert result.overture_release == sources.settings.overture_release


def test_both_failing_raises_airport_overpass_error(monkeypatch):
    _install(
        monkeypatch,
        _Overture(error=RuntimeError("S3 unreachable")),
        _Overpass(fail_full=True, fail_reduced=True),
    )

    with pytest.raises(AirportOverpassError):
        _load()


def test_overture_disabled_uses_full_overpass_only(monkeypatch):
    full = [
        _way("runway", _line_m((-200, 0), (200, 0)), way_id=1),
        _way("parking_position", [_ll(400, 0), _ll(410, 0)], way_id=2),
    ]
    overture = _Overture([_overture_row("runway", _line_m((-200, 0), (200, 0)))])
    overpass = _Overpass(full=full)
    _install(monkeypatch, overture, overpass)
    monkeypatch.setattr(sources.settings, "overture_enabled", False)

    result = _load()

    assert overture.calls == []
    assert overpass.queries == [build_airport_query(LAT, LON, R)]
    assert result.warnings == []
    assert result.overture_release is None
    assert result.sources == {cls: "osm" for cls in AEROWAY_CLASSES}
    assert len(result.geoms["stands"]) == 1


def test_invalid_release_is_rejected_before_any_network_call():
    with pytest.raises(ValueError):
        sources.read_overture_airfield(LAT, LON, R, "latest; DROP TABLE x")


@pytest.mark.parametrize(
    "bad",
    ["x'; DROP", "2026-10-21.0\n", " 2026-10-21.0", "2026-10-21.0 ", "latest"],
)
def test_release_must_fully_match_before_sql_is_built(bad):
    with pytest.raises(ValueError):
        sources.build_overture_sql(LAT, LON, R, bad)
    with pytest.raises(ValueError):
        sources.read_overture_airfield(LAT, LON, R, bad)


def test_overture_sql_pushes_bbox_and_subtype_down():
    sql = sources.build_overture_sql(LAT, LON, R, "2026-10-21.0")
    west, south, east, north = sources._bbox(LAT, LON, R)

    assert f"bbox.xmin <= {east:.7f}" in sql
    assert f"bbox.xmax >= {west:.7f}" in sql
    assert f"bbox.ymin <= {north:.7f}" in sql
    assert f"bbox.ymax >= {south:.7f}" in sql
    assert "subtype = 'airport'" in sql
    assert (
        "class in ('runway', 'taxiway', 'taxilane', 'apron', 'stopway')" in sql
    )
    assert (
        "read_parquet('s3://overturemaps-us-west-2/release/2026-10-21.0/"
        "theme=base/type=infrastructure/*'" in sql
    )
    assert sql.rstrip().endswith("order by id"), "row order must be deterministic"


@pytest.mark.parametrize(
    "message",
    [
        "HTTP Error: HTTP GET error on 'https://x/y' (HTTP 404)",
        "HTTP 404 Not Found for release 2099-01-01.0",
        "HTTP/1.1 404 Not Found",
        "NoSuchKey: The specified key does not exist.",
        "No files found that match the pattern",
    ],
)
def test_missing_release_errors_are_recognised(message):
    assert sources._is_not_found(RuntimeError(message))


@pytest.mark.parametrize(
    "message",
    [
        "S3 unreachable",
        "connection not found in pool",
        "read timed out after 404 ms",
        "http://mirror:8404 refused",
        "the requested layer was not found in the cache",
    ],
)
def test_other_errors_are_not_treated_as_a_missing_release(message):
    assert not sources._is_not_found(RuntimeError(message))


# --- Filters -----------------------------------------------------------------


def test_line_crossing_the_circle_is_kept_with_no_endpoint_inside(monkeypatch):
    # Both endpoints are 2000 m from the centre; the segment passes through it.
    crossing = _line_m((-2000, 0), (2000, 0))
    _install(monkeypatch, _Overture([_overture_row("apron", crossing)]), _Overpass())

    assert _load().geoms["apron"] == [crossing]


def test_polygon_enclosing_the_centre_is_kept(monkeypatch):
    # Every edge is 2000 m away, so only the point-in-ring test keeps it.
    ring = _line_m((-2000, -2000), (2000, -2000), (2000, 2000), (-2000, 2000), (-2000, -2000))
    far_ring = _line_m((3000, 3000), (3500, 3000), (3500, 3500), (3000, 3000))
    overture = _Overture(
        [_overture_row("apron", ring), _overture_row("apron", far_ring)]
    )
    _install(monkeypatch, overture, _Overpass())

    assert _load().geoms["apron"] == [ring]


# --- Empty result, STAC fallback and the outage window -----------------------


def test_empty_overture_rows_fall_back_to_full_overpass(monkeypatch):
    full = [_way("runway", _line_m((-200, 0), (200, 0)), way_id=1)]
    overpass = _Overpass(full=full)
    _install(monkeypatch, _Overture([]), overpass)

    result = _load()

    assert result.warnings == ["overture_empty"]
    assert result.sources == {cls: "osm" for cls in AEROWAY_CLASSES}
    assert result.overture_release is None
    assert len(result.geoms["runway"]) == 1
    assert build_airport_query(LAT, LON, R) in overpass.queries
    assert sources._overture_down_until == 0.0, "an empty read is not an outage"


class _ByRelease:
    """Overture reader where only some releases exist; others 404."""

    def __init__(self, available: dict[str, list]):
        self.available = dict(available)
        self.calls: list[str] = []

    def __call__(self, lat, lon, radius_m, release):
        self.calls.append(release)
        if release not in self.available:
            raise RuntimeError(f"HTTP 404 Not Found for release {release}")
        return list(self.available[release])


def _install_stac(monkeypatch, latest: str | None = None, error: Exception | None = None):
    """Replace ``httpx.AsyncClient`` so the STAC catalog answers ``latest``."""

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"latest": latest}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            if error is not None:
                raise error
            return _Resp()

    monkeypatch.setattr(sources.httpx, "AsyncClient", _Client)


def test_pinned_release_404_retries_with_the_stac_latest(monkeypatch):
    pinned = sources.settings.overture_release
    latest = "2099-01-01.0"
    overture = _ByRelease(
        {latest: [_overture_row("runway", _line_m((-200, 0), (200, 0)))]}
    )
    _install(monkeypatch, overture, _Overpass())
    _install_stac(monkeypatch, latest=latest)

    result = _load()

    assert overture.calls == [pinned, latest]
    assert result.overture_release == latest
    assert result.sources["runway"] == "overture"
    assert result.warnings == []


def test_stac_failure_falls_back_to_full_overpass(monkeypatch):
    pinned = sources.settings.overture_release
    overture = _ByRelease({})
    full = [_way("runway", _line_m((-200, 0), (200, 0)), way_id=1)]
    overpass = _Overpass(full=full)
    _install(monkeypatch, overture, overpass)
    _install_stac(monkeypatch, error=httpx.ConnectError("no route to STAC"))

    result = _load()

    assert overture.calls == [pinned]
    assert result.warnings == ["overture_unavailable"]
    assert result.overture_release is None
    assert build_airport_query(LAT, LON, R) in overpass.queries


def test_overture_outage_is_skipped_for_the_backoff_window(monkeypatch):
    overture = _Overture(error=RuntimeError("S3 unreachable"))
    full = [_way("runway", _line_m((-200, 0), (200, 0)), way_id=1)]
    overpass = _Overpass(full=full)
    _install(monkeypatch, overture, overpass)

    assert _load().warnings == ["overture_unavailable"]
    assert len(overture.calls) == 1

    second = _load()

    assert second.warnings == ["overture_unavailable"]
    assert len(overture.calls) == 1, "inside the window Overture is not read"
    assert overpass.queries[2:] == [build_airport_query(LAT, LON, R)]

    monkeypatch.setattr(sources, "_overture_down_until", 0.0)
    _load()
    assert len(overture.calls) == 2, "after the window Overture is tried again"


def test_overture_and_reduced_overpass_run_concurrently(monkeypatch):
    """Sequential calls would leave Overture waiting for the reduced query
    for its full 2 s and report no overlap."""
    reduced_started = threading.Event()
    seen: dict[str, bool] = {}

    class _Probe(_Overture):
        def __call__(self, lat, lon, radius_m, release):
            seen["overlap"] = reduced_started.wait(timeout=2.0)
            return super().__call__(lat, lon, radius_m, release)

    class _Signal(_Overpass):
        async def __call__(self, query, client=None):
            if "parking_position" in query:
                reduced_started.set()
            return await super().__call__(query, client=client)

    overture = _Probe([_overture_row("runway", _line_m((-200, 0), (200, 0)))])
    _install(monkeypatch, overture, _Signal())

    result = _load()

    assert seen["overlap"] is True
    assert result.warnings == []
