"""Concurrent writes to the traffic slice store must not corrupt a slice (#95).

Two ingests of the same day legitimately write the same key
(``day/{date}/{icao}.json.gz``). With a fixed ``<name>.tmp`` the two writers
share one temp path, their bytes interleave and ``os.replace`` publishes a
truncated gzip slice -> ``502 traffic_store_unavailable`` on
``GET /v1/traffic/render`` and a ``gzip.decompress`` crash on the ingest
cache-hit path.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
from pathlib import Path

from backend.traffic import store as store_module
from backend.traffic.store import FileSliceStore, day_key


def _payload(icao: str, n: int) -> bytes:
    return gzip.compress(json.dumps({"icao": icao, "flights": list(range(n))}).encode())


def test_concurrent_writers_publish_a_valid_gzip_slice(tmp_path):
    store = FileSliceStore(tmp_path / "traffic")
    key = day_key("2026-10-10", "LHBP")
    # Big enough that the two writes overlap in time.
    payloads = [_payload("LHBP", 20000), _payload("LHBP", 20001)]

    barrier = threading.Barrier(len(payloads))
    errors: list[BaseException] = []

    def writer(data: bytes) -> None:
        try:
            barrier.wait(timeout=10)
            store.put(key, data)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    published = store.get(key)
    assert published is not None
    # The published file must be complete gzip, not an interleaved mix.
    assert gzip.decompress(published) in {
        gzip.decompress(p) for p in payloads
    }
    # No temp file is left behind.
    assert list((tmp_path / "traffic").rglob("*.tmp")) == []


def test_store_put_uses_a_unique_temp_name(tmp_path, monkeypatch):
    store = FileSliceStore(tmp_path / "traffic")
    key = day_key("2026-10-10", "LHBP")

    tmp_names: list[str] = []
    real_replace = os.replace

    def spy(src, dst, **kwargs):  # noqa: ANN001, ANN202 - test helper
        tmp_names.append(Path(src).name)
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(store_module.os, "replace", spy)

    store.put(key, b"a")
    store.put(key, b"b")

    assert len(tmp_names) == 2
    assert tmp_names[0] != tmp_names[1]
    assert tmp_names[0].startswith("LHBP.json.gz.")
    assert tmp_names[0].endswith(".tmp")
    # The published content is the last writer's bytes.
    assert store.get(key) == b"b"
