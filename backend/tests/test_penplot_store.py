"""Unit tests for the filesystem store's lazy TTL (reviews C.2.1, C.2.3).

Plus the P1 result-sidecar: the convert endpoint serves repeated identical
converts from the sidecar without recomputing, so the store's meta round-trip
and read cost are contract here.
"""

from __future__ import annotations

import os
import time

from backend.penplot.store import ImageStore
from backend.tests.helpers import png_bytes, svg_bytes


def _store(tmp_path) -> ImageStore:
    return ImageStore(
        images_dir=str(tmp_path / "images"),
        results_dir=str(tmp_path / "results"),
        ttl_hours=48,
    )


def test_image_expired_file_removed_on_find(tmp_path):
    store = _store(tmp_path)
    image_id = store.put_image_bytes(png_bytes(), "png")
    path = store.find_image(image_id)
    assert path is not None and os.path.exists(path)
    # Rewind mtime beyond TTL, as if 48h elapsed.
    os.utime(path, (time.time() - 49 * 3600, time.time() - 49 * 3600))
    assert store.find_image(image_id) is None
    assert not os.path.exists(path)


def test_put_refreshes_expired_image(tmp_path):
    store = _store(tmp_path)
    image_id = store.put_image_bytes(svg_bytes(), "svg")
    path = store.find_image(image_id)
    os.utime(path, (time.time() - 49 * 3600, time.time() - 49 * 3600))
    # Re-upload of identical bytes slides the TTL instead of hard-failing.
    assert store.put_image_bytes(svg_bytes(), "svg") == image_id
    assert store.find_image(image_id) is not None


def test_result_ttl_expired_file_removed_on_read(tmp_path):
    store = _store(tmp_path)
    filename = "0" * 64 + "_" + "0" * 12 + "_optimized.svg"
    path = store.put_result(filename, "<svg/>")
    assert os.path.exists(path)
    os.utime(path, (time.time() - 49 * 3600, time.time() - 49 * 3600))
    # Review C.2.3: results share the images' lazy TTL — an expired result is
    # dropped on access so the caller 404s and re-runs the convert.
    assert store.has_result(filename) is False
    assert store.result_path(filename) is not None  # still the safe path
    assert not os.path.exists(path)
    assert not os.listdir(store.results_dir)


def test_result_path_guards_traversal(tmp_path):
    store = _store(tmp_path)
    path = store.result_path("../../etc/passwd")
    assert path == os.path.join(store.results_dir, "passwd") or ".." not in path


# -- result sidecar (P1 cache-first conversions) --------------------------

def _filename() -> str:
    return "0" * 64 + "_" + "0" * 12 + "_optimized.svg"


def test_result_meta_roundtrip(tmp_path):
    store = _store(tmp_path)
    filename = _filename()
    store.put_result(filename, "<svg/>")
    path = store.put_result_meta(filename, {
        "stats": {"points": {"before": 10, "after": 4}, "strokes": 2,
                  "pen_down_mm": 1.25, "pen_up_mm": 0.0, "estimated_time_s": 0.5},
        "warnings": ["travel_optimization_off"],
        "vpype_command": "read --quantization 0.02mm",
    })
    assert path == store.result_meta_path(filename)
    meta = store.get_result_meta(filename)
    assert meta is not None
    assert meta["warnings"] == ["travel_optimization_off"]
    assert meta["stats"]["strokes"] == 2
    assert meta["vpype_command"].startswith("read")


def test_result_meta_missing_or_corrupt_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store.get_result_meta(_filename()) is None
    filename = _filename()
    store.put_result(filename, "<svg/>")
    store.put_result_meta(filename, {"nope": True})
    meta = store.get_result_meta(filename)
    assert meta is not None and meta == {"nope": True}
    with open(store.result_meta_path(filename), "w") as fh:
        fh.write("{not json")
    assert store.get_result_meta(filename) is None


def test_result_meta_read_is_fast(tmp_path):
    # P1 perf guard: a cache hit must not re-run the pipeline — reading the
    # sidecar plus statting the SVG stays well under 10 ms regardless of how
    # heavy the convert that produced it was.
    store = _store(tmp_path)
    filename = _filename()
    store.put_result(filename, "<svg/>")
    store.put_result_meta(filename, {"vpype_command": "x", "warnings": [],
                                     "stats": {"a": 1}})
    t0 = time.perf_counter()
    for _ in range(50):
        assert store.get_result_meta(filename) is not None
        assert store.has_result(filename) is True
    elapsed = (time.perf_counter() - t0) / 50
    assert elapsed < 0.01