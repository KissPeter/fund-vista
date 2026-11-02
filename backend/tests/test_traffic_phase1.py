"""Traffic Phase 1 hermetic tests (plan TC-D43): classifier + filters + ingest-from-tar.

No network: synthetic traces/tars only (httpx MockTransport for the
availability index call). Ports the POC ``tools/airport_traffic/tests``
cases that belong to Phase 1 (render/basemap cases move with Phase 2).
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import math
import tarfile

import httpx
import pytest
from pydantic import ValidationError

from backend.traffic import availability as _avail
from backend.traffic import classify, ingest
from backend.traffic.classify import CLASSIFIER_VERSION
from backend.traffic.filters import Filters, pen
from backend.traffic.hungary import is_hungarian_icao, row_is_hungarian
from backend.traffic.schemas import TrafficIngestRequest, movements_key
from backend.traffic.store import FileSliceStore, day_key, pass_key

LAT, LON, ELEV = 47.4369, 19.2556, 495.0
AP = {"lat": LAT, "lon": LON, "elev_ft": ELEV}


def _leg(points, t0=1_000_000.0, step=10.0):
    return [[t0 + i * step, la, lo, alt, 1 if alt == 0 else 0] for i, (la, lo, alt) in enumerate(points)]


def _approach(n=60, start_km=60.0):
    pts = []
    for i in range(n):
        f = 1 - i / (n - 1)
        d = start_km * f / 111.2
        alt = 0 if i == n - 1 else ELEV + 300 + 9000 * f
        pts.append((LAT + d * 0.7, LON - d * 0.7, alt))
    return pts


def _day(*flights):
    return {"airport": AP, "flights": [dict(hex="a%d" % i, reg=None, type=t, desc=None, operator=None,
                                            calls=[[0, cs]], pts=pts) for i, (cs, t, pts) in enumerate(flights)]}


def test_arrival_and_departure():
    arr = _leg(_approach())
    dep = _leg(list(reversed(_approach())), t0=2_000_000.0)
    moves = classify.classify_day(_day(("WZZ1", "A321", arr), ("RYR2", "B738", dep)))
    assert [m["kind"] for m in moves] == ["arr", "dep"]
    assert not any(m["holding"] or m["go_around"] for m in moves)


def test_holding_detected_as_full_turn_outside_final():
    base = _approach(n=60)
    hold = []
    cx, cy = LAT + 0.25, LON - 0.35  # ~30 km out
    for k in range(37):  # one full circle, 3 km radius, ~10 s apart
        a = math.radians(k * 10)
        hold.append((cx + 0.027 * math.cos(a), cy + 0.04 * math.sin(a), ELEV + 5000))
    leg = _leg(base[:30] + hold + base[30:])
    (m,) = classify.classify_day(_day(("HOLD1", "A320", leg)))
    assert m["kind"] == "arr" and m["holding"] and not m["go_around"]


def test_go_around_detected():
    base = _approach(n=60)
    low = base[:55]
    climb = [(LAT - 0.02 * i / 5, LON + 0.02 * i / 5, ELEV + 800 + 400 * i) for i in range(10)]
    back = _approach(n=40, start_km=30)[:-1] + [(LAT, LON, 0)]
    leg = _leg(low + climb + back)
    (m,) = classify.classify_day(_day(("GA1", "E190", leg)))
    assert m["go_around"]


def test_normal_arrival_is_not_flagged():
    (m,) = classify.classify_day(_day(("OK1", "A320", _leg(_approach()))))
    assert not m["go_around"] and not m["holding"]


# -- traces with null altitudes (real adsb.lol data) ------------------------

def _leg_null_alt(points, t0=1_000_000.0, step=10.0):
    """Leg whose cruise points carry a ``null`` altitude (unknown height)."""
    return [[t0 + i * step, la, lo, alt, 1 if alt == 0 else 0] for i, (la, lo, alt) in enumerate(points)]


def test_null_altitude_cruise_still_classifies_the_ground_contact():
    # Unknown altitudes must not crash and must not hide a real arrival:
    # the ground contact (alt 0) still proves the movement.
    base = _approach()
    pts = [(la, lo, None if i % 2 else alt) for i, (la, lo, alt) in enumerate(base)]
    moves = classify.classify_day(_day(("NUL1", "A321", _leg_null_alt(pts))))
    assert [m["kind"] for m in moves] == ["arr"]


def test_null_altitude_only_trace_yields_no_movement():
    # Every point's altitude is unknown: nothing proves "low", so no
    # movement is invented (and no crash).
    pts = [(la, lo, None) for la, lo, _alt in _approach()]
    moves = classify.classify_day(_day(("NUL2", "A321", _leg_null_alt(pts))))
    assert moves == []


def test_null_altitude_go_around_scan_does_not_crash():
    base = _approach(n=60)
    low = base[:55]
    climb = [(LAT - 0.02 * i / 5, LON + 0.02 * i / 5, ELEV + 800 + 400 * i) for i in range(10)]
    back = _approach(n=40, start_km=30)[:-1] + [(LAT, LON, 0)]
    pts = [(la, lo, None if i % 3 else alt) for i, (la, lo, alt) in enumerate(low + climb + back)]
    moves = classify.classify_day(_day(("NUL3", "E190", _leg_null_alt(pts))))
    assert isinstance(moves, list)


def _movs():
    base = {"hex": "x", "type": "A320", "t": 5.0, "pts": [[0, LAT, LON, 0, 1]], "db_flags": 0}
    return [dict(base, flight="WZZ1", kind="arr", holding=False, go_around=False),
            dict(base, flight="RYR2", kind="dep", holding=False, go_around=False, type="B738"),
            dict(base, flight="WZZ3", kind="arr", holding=True, go_around=False),
            dict(base, flight="DLH4", kind="arr", holding=False, go_around=True)]


def _flt(**kw):
    return Filters(t_from=0, t_to=10, **kw)


def test_filter_semantics():
    m = _movs()
    assert [pen(f, _flt()) for f in m] == ["arrivals", "departures", "holding", "go_around"]
    only_ga = _flt(arrivals=False, departures=False, holding=False)
    assert [pen(f, only_ga) for f in m] == [None, None, None, "go_around"]
    no_hold_pen = _flt(holding=False)
    assert pen(m[2], no_hold_pen) == "arrivals"  # still an arrival, but no highlight pen
    assert [pen(f, _flt(types={"B738"})) for f in m] == [None, "departures", None, None]
    assert [pen(f, _flt(airlines={"wzz"})) for f in m] == ["arrivals", None, "holding", None]


def test_callsign_is_case_insensitive_substring():
    """Callsign is a substring match (any position), case-insensitive."""
    m = _movs()  # WZZ1 (arr), RYR2 (dep), WZZ3 (arr, holding), DLH4 (arr, go-around)
    assert [pen(f, _flt(callsign="zz")) for f in m] == ["arrivals", None, "holding", None]
    assert [pen(f, _flt(callsign="wzz1")) for f in m] == ["arrivals", None, None, None]
    assert [pen(f, _flt(callsign="ZZ1")) for f in m] == ["arrivals", None, None, None]  # middle/end
    assert [pen(f, _flt(callsign="RYR")) for f in m] == [None, "departures", None, None]
    assert [pen(f, _flt(callsign="")) for f in m] == [pen(f, _flt()) for f in m]  # empty = no filter
    assert pen(dict(m[0], flight=None), _flt(callsign="W")) is None  # no callsign never matches
    assert pen(dict(m[0], flight=None), _flt()) == "arrivals"


def test_callsign_is_not_the_airline_prefix_match():
    """Contract pin: airlines match the callsign START, callsign matches anywhere."""
    m = _movs()
    assert [pen(f, _flt(airlines={"ZZ"})) for f in m] == [None, None, None, None]
    assert [pen(f, _flt(callsign="ZZ")) for f in m] == ["arrivals", None, "holding", None]


def test_pen_type_match_is_case_sensitive():
    """pen() compares types exactly; the request schema is what normalises case."""
    m = _movs()[0]  # type A320
    assert pen(m, _flt(types={"A320"})) == "arrivals"
    assert pen(m, _flt(types={"a320"})) is None


def test_empty_type_element_hides_everything_absent_types_show_all():
    """The bug's two meanings: {""} hides every movement, an absent filter shows all."""
    m = _movs()
    assert [pen(f, _flt(types={""})) for f in m] == [None, None, None, None]
    assert [pen(f, _flt()) for f in m] == ["arrivals", "departures", "holding", "go_around"]


def test_military_and_privacy_excluded_by_default():
    m = dict(_movs()[0])
    assert pen(dict(m, db_flags=1), _flt()) is None  # military
    assert pen(dict(m, db_flags=4), _flt()) is None  # PIA
    assert pen(dict(m, db_flags=8), _flt()) is None  # LADD
    assert pen(dict(m, db_flags=1), _flt(exclude_military=False)) == "arrivals"
    assert pen(dict(m, db_flags=4), _flt(exclude_privacy=False)) == "arrivals"


def test_window_limits():
    with pytest.raises(ValueError):
        Filters(t_from=0, t_to=24 * 3600 + 1)
    with pytest.raises(ValueError):
        Filters(t_from=10, t_to=10)
    assert pen(_movs()[0], Filters(t_from=6, t_to=9)) is None


def _tar(files: dict[str, dict]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, doc in files.items():
            raw = gzip.compress(json.dumps(doc).encode())
            ti = tarfile.TarInfo(name)
            ti.size = len(raw)
            tf.addfile(ti, io.BytesIO(raw))
    return buf.getvalue()


def test_extract_keeps_only_airport_traffic_across_chunk_boundaries():
    near = {"icao": "aaa111", "r": "HA-LXA", "t": "A320", "timestamp": 1000.0,
            "trace": [[0, LAT + 0.2, LON, 4000, 150, 0, 0, 0, {"flight": "WZZ1  "}],
                      [60, LAT, LON, "ground", 10, 0, 0, 0, None]]}
    far = {"icao": "bbb222", "timestamp": 1000.0, "trace": [[0, 50.0, 10.0, 35000, 400, 0, 0, 0, None]]}
    blob = _tar({"traces/aa/trace_full_aaa111.json": near, "traces/bb/trace_full_bbb222.json": far,
                 "readme.txt": {}})
    chunks = iter([blob[i:i + 777] for i in range(0, len(blob), 777)])
    out = ingest.extract(io.BufferedReader(ingest.ChunkReader(chunks)), LAT, LON, ELEV, (45.0, 49.0, 15.0, 23.0))
    assert [f["hex"] for f in out] == ["aaa111"]
    f = out[0]
    assert f["calls"] == [[1000.0, "WZZ1"]] and f["type"] == "A320"
    assert f["pts"][-1][3:] == [0, 1]


def test_extract_multi_splits_one_pass_per_airport():
    bud = {"icao": "aaa111", "timestamp": 1000.0,
           "trace": [[0, LAT, LON, "ground", 10, 0, 0, 0, None]]}
    vie_lat, vie_lon = 48.1103, 16.5697
    vie = {"icao": "bbb222", "timestamp": 1000.0,
           "trace": [[0, vie_lat, vie_lon, "ground", 10, 0, 0, 0, None]]}
    blob = _tar({"traces/aa/trace_full_aaa111.json": bud, "traces/bb/trace_full_bbb222.json": vie})
    specs = [
        {"icao": "LHBP", "lat": LAT, "lon": LON, "elev_ft": ELEV,
         "bbox": (45.0, 49.0, 15.0, 23.0)},
        {"icao": "LOWW", "lat": vie_lat, "lon": vie_lon, "elev_ft": 600.0,
         "bbox": (46.0, 50.0, 14.0, 19.0)},
    ]
    out = ingest.extract_multi(io.BytesIO(blob), specs)
    assert [f["hex"] for f in out["LHBP"]] == ["aaa111"]
    assert [f["hex"] for f in out["LOWW"]] == ["bbb222"]


def test_extract_multi_cancel_raises():
    blob = _tar({"traces/aa/trace_full_x.json":
                 {"icao": "x", "timestamp": 1.0, "trace": [[0, LAT, LON, "ground", 0, 0, 0, 0, None]]}})
    specs = [{"icao": "LHBP", "lat": LAT, "lon": LON, "elev_ft": ELEV, "bbox": (45.0, 49.0, 15.0, 23.0)}]
    with pytest.raises(RuntimeError, match="cancelled"):
        ingest.extract_multi(io.BytesIO(blob), specs, cancelled=lambda: True)


TODAY = dt.date(2026, 10, 11)


def _client(status: int, body: dict | None = None) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, json=body or {})))


def test_range_is_last_year_up_to_yesterday():
    assert _avail.selectable_range(TODAY) == (dt.date(2025, 10, 11), dt.date(2026, 10, 10))


def test_out_of_range_never_calls_the_api():
    c = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError("called"))))
    assert _avail.source_status(TODAY, c, TODAY)["status"] == "out_of_range"
    assert _avail.source_status(dt.date(2025, 10, 1), c, TODAY)["status"] == "out_of_range"


def test_available_and_missing_release():
    ok = _client(200, {"assets": [{"size": 2_000_000_000}, {"size": 1_550_429_696}]})
    s = _avail.source_status(dt.date(2026, 10, 10), ok, TODAY)
    assert s["status"] == "available" and s["parts"] == 2 and s["bytes"] == 3_550_429_696
    assert s["tag"] == "v2026.10.10-planes-readsb-prod-0"
    assert _avail.source_status(dt.date(2026, 5, 6), _client(404), TODAY)["status"] == "unavailable"


def test_ingest_request_validation():
    assert TrafficIngestRequest(icao="lhbp", date="2026-10-10").icao == "LHBP"
    with pytest.raises(ValidationError):
        TrafficIngestRequest(icao="LHBP", date="10/10/2026")
    with pytest.raises(ValidationError):
        TrafficIngestRequest(icao="", date="2026-10-10")
    with pytest.raises(ValidationError):
        TrafficIngestRequest(icao="LHBP", date="2026-10-10", extra_field="x")


def test_hungarian_scope_prefix_and_row():
    assert is_hungarian_icao("LHBP") and is_hungarian_icao(" lhdc ")
    assert not is_hungarian_icao("EGLL") and not is_hungarian_icao("KJFK") and not is_hungarian_icao("")
    assert row_is_hungarian({"iso_country": "HU"})
    assert not row_is_hungarian({"iso_country": "DE"})
    assert not row_is_hungarian({})


def test_ingest_request_rejects_non_hungarian_icao():
    with pytest.raises(ValidationError, match="Hungarian"):
        TrafficIngestRequest(icao="EGLL", date="2026-10-10")


def test_movements_key_carries_classifier_version():
    assert CLASSIFIER_VERSION in movements_key("LHBP", "2026-10-10")


def test_slice_store_roundtrip_and_traversal_guard(tmp_path):
    store = FileSliceStore(tmp_path / "traffic")
    key = day_key("2026-10-10", "lhbp")
    assert key == "day/2026-10-10/LHBP.json.gz"
    assert pass_key("2026-10-10") == "pass/2026-10-10.json"
    assert not store.exists(key) and store.get(key) is None
    store.put(key, b"bytes")
    assert store.exists(key) and store.get(key) == b"bytes"
    store.put(key, b"bytes2")  # atomic overwrite
    assert store.get(key) == b"bytes2"
    with pytest.raises(ValueError):
        store.get("../escape.json")
