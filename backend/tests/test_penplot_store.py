"""Unit tests for the filesystem store's lazy TTL (reviews C.2.1, C.2.3)."""

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