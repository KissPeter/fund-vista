"""Unit tests for ``backend.traffic.ingest.extract_multi`` (pure algorithm).

No app import, no network: the tar stream is built in-memory the same way
the adsb.lol day archive lays out members (``trace_full_*.json[.gz]``).
Doc partitions:

* near one airport (ground, < NEAR_KM) → kept for that airport only;
* far outside every airport bbox → kept for none (prefilter skips);
* inside a bbox but never near the ground/low → kept for none (the
  prefilter must pass it through to ``_near_points``, not decide itself);
* inside a bbox, at ground level, but beyond NEAR_KM → kept for none;
* empty / malformed doc → ignored.

The bbox prefilter is exact, not heuristic: a point within ``NEAR_KM`` of
an airport always lies inside that airport's ±span bbox (the airport is
the bbox centre), so a rect miss implies ``_near_points`` false.
"""

import gzip
import io
import json
import tarfile

from backend.traffic.ingest import extract_multi, default_bbox

# Two HU airports far enough apart that no doc's trace can be near both.
LHBP = {"icao": "LHBP", "lat": 47.4369, "lon": 19.2556, "elev_ft": 495.0,
        "bbox": default_bbox(47.4369, 19.2556)}
LHDC = {"icao": "LHDC", "lat": 47.4843, "lon": 21.6171, "elev_ft": 354.0,
        "bbox": default_bbox(47.4843, 21.6171)}
SPECS = [LHBP, LHDC]


def _doc(hex_id: str, points: list) -> dict:
    return {"icao": hex_id, "r": "HA-ABC", "t": "C172", "timestamp": 1760000000,
            "trace": points}


def _point(t: float, lat: float, lon: float, alt_ft: float, ground: bool = False) -> list:
    return [t, lat, lon, "ground" if ground else alt_ft]


def _tar_stream(docs: list[dict]) -> io.BufferedReader:
    """Serialise docs as individually-gzipped trace_full_* tar members."""
    raw = io.BytesIO()
    tf = tarfile.open(fileobj=raw, mode="w")
    for i, d in enumerate(docs):
        payload = gzip.compress(json.dumps(d).encode("utf-8"))
        ti = tarfile.TarInfo(f"traces/{i % 256:02x}/trace_full_{d['icao']}.json")
        ti.size = len(payload)
        tf.addfile(ti, io.BytesIO(payload))
    tf.close()
    raw.seek(0)
    return io.BufferedReader(raw, 1 << 16)


def _run(docs: list[dict], specs=SPECS, cancelled=None) -> dict:
    return extract_multi(_tar_stream(docs), specs, cancelled=cancelled)


def test_doc_near_one_airport_kept_for_that_airport_only():
    bp = _doc("aabbcc", [_point(0, 47.44, 19.26, 0, ground=True)])
    dc = _doc("ddeeff", [_point(0, 47.49, 21.62, 0, ground=True)])
    out = _run([bp, dc])
    assert [f["hex"] for f in out["LHBP"]] == ["aabbcc"]
    assert [f["hex"] for f in out["LHDC"]] == ["ddeeff"]


def test_doc_outside_every_bbox_skipped():
    # Far from both airports — outside both bboxes, so the prefilter must
    # decide it without the near-points scan.
    far = _doc("aabbcc", [_point(0, -47.44, -19.26, 0, ground=True)])
    out = _run([far])
    assert out == {"LHBP": [], "LHDC": []}


def test_doc_inside_bbox_but_cruising_high_is_rejected():
    # 20 000 ft over the field: inside the bbox (prefilter passes) but
    # never near the ground/low — must still be rejected by the near scan.
    high = _doc("aabbcc", [_point(0, 47.44, 19.26, 20000)])
    out = _run([high])
    assert out == {"LHBP": [], "LHDC": []}


def test_doc_inside_bbox_on_ground_beyond_near_km_is_rejected():
    # ~16 km south of the field: ground level, inside the bbox, but beyond
    # the 12 km near-radius — the near scan rejects it.
    far_ground = _doc("aabbcc", [_point(0, 47.4369 - 0.15, 19.2556, 0, ground=True)])
    out = _run([far_ground])
    assert out == {"LHBP": [], "LHDC": []}


def test_empty_and_malformed_ignored():
    out = _run([_doc("aabbcc", []), {"icao": "bad"}])
    assert out == {"LHBP": [], "LHDC": []}


def test_trace_with_only_none_positions_ignored():
    out = _run([_doc("aabbcc", [_point(0, None, None, 0)])])
    assert out == {"LHBP": [], "LHDC": []}


def test_cancelled_raises_between_docs():
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 1  # first check passes, second trips

    docs = [_doc(f"aabbc{i}", [_point(0, 47.44, 19.26, 0, ground=True)]) for i in range(3)]
    try:
        _run(docs, cancelled=cancelled)
        raised = False
    except RuntimeError as exc:
        raised = str(exc) == "cancelled"
    assert raised


# -- parallel pass -----------------------------------------------------------

def _doc_set(n: int) -> list[dict]:
    """Docs spread over both airports, plus far/high/edge rejects."""
    docs = []
    for i in range(n):
        if i % 3 == 0:
            docs.append(_doc(f"bp{i:04d}", [_point(0, 47.44, 19.26, 0, ground=True),
                                            _point(1, 47.4370, 19.2557, 1200)]))
        elif i % 3 == 1:
            docs.append(_doc(f"dc{i:04d}", [_point(0, 47.49, 21.62, 0, ground=True)]))
        else:
            docs.append(_doc(f"xx{i:04d}", [_point(0, -47.44, -19.26, 0, ground=True),
                                            _point(1, 47.44, 19.26, 20000)]))
    return docs


def test_parallel_pass_equals_serial_pass(monkeypatch):
    """The pool path must return exactly what the serial pass returns."""
    docs = _doc_set(120)
    serial = extract_multi(_tar_stream(docs), SPECS)
    # The pool path reads the tar members itself — break the serial reader
    # so a silent fallback to it would fail the test.
    monkeypatch.setattr("backend.traffic.ingest._iter_docs",
                        lambda stream: (_ for _ in ()).throw(AssertionError("serial path used")))
    for workers, batch in ((2, 7), (4, 1), (3, 1000)):
        parallel = extract_multi(_tar_stream(docs), SPECS, workers=workers, batch_members=batch)
        assert parallel == serial, f"workers={workers} batch={batch} diverged"


def test_parallel_pass_keeps_archive_order():
    """Batches merge in archive order, so the flight list order is stable."""
    docs = _doc_set(30)
    out = extract_multi(_tar_stream(docs), SPECS, workers=4, batch_members=2)
    assert [f["hex"] for f in out["LHBP"]] == [f["hex"] for f in out["LHBP"]]
    serial = extract_multi(_tar_stream(docs), SPECS)
    assert [f["hex"] for f in out["LHBP"]] == [f["hex"] for f in serial["LHBP"]]


def test_parallel_pass_cancelled_raises():
    docs = _doc_set(40)
    try:
        extract_multi(_tar_stream(docs), SPECS, workers=2, batch_members=1,
                      cancelled=lambda: True)
        raised = False
    except RuntimeError as exc:
        raised = str(exc) == "cancelled"
    assert raised


def test_parallel_pass_empty_archive():
    out = extract_multi(_tar_stream([]), SPECS, workers=2)
    assert out == {"LHBP": [], "LHDC": []}
