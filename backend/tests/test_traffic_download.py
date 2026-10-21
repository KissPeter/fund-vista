"""Parallel archive download (issue #66 follow-up 1) + progress counters.

Two layers:

* ``backend.traffic.ingest._parallel_chunks`` — the downloader itself, over
  **real HTTP** against a local socket server (a real threaded
  ``http.server``, real bytes, real concurrency). It must yield the parts in
  archive order, byte-identical to the serial pass, fetch them concurrently,
  and surface a failing part as an exception.
* the job's progress counters — the ``progress`` field of ``GET /v1/jobs/{id}``
  must carry the download/parse counters, not just ``stage=download``.

No network access beyond 127.0.0.1; the archive upstream is replaced by the
local server, the job's airport lookup by the existing test stub.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("PENPLOT_DATA_DIR", tempfile.mkdtemp(prefix="penplot-traffic-dl-"))
os.environ["REDIS_CLOUD_URL"] = "redis://127.0.0.1:1/0"
os.environ["PENPLOT_RATE_LIMIT"] = "100000"
os.environ["TRAFFIC_STORE"] = "file"

import httpx
import pytest

from backend.traffic import ingest

PARTS = [b"alpha-part;" * 1000, b"beta-part;" * 1000, b"gamma-part;" * 1000]
PART_DELAY_S = 0.25  # a part answers slowly → serial download would take 3x


class _Handler(BaseHTTPRequestHandler):
    """Serves the three archive parts; records the overlap of the requests."""

    protocol_version = "HTTP/1.1"
    active = 0
    max_active = 0
    lock = threading.Lock()
    started: list[float] = []

    def do_GET(self):  # noqa: N802
        tail = self.path.rsplit("/", 1)[-1]
        if not tail.isdigit():
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        idx = int(tail)
        body = PARTS[idx]
        with _Handler.lock:
            _Handler.active += 1
            _Handler.max_active = max(_Handler.max_active, _Handler.active)
            _Handler.started.append(time.monotonic())
        try:
            time.sleep(PART_DELAY_S)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            with _Handler.lock:
                _Handler.active -= 1

    def log_message(self, *args):  # noqa: D102 - silence the default stderr log
        pass


@pytest.fixture(scope="module")
def archive_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _reset_server_state():
    _Handler.active = 0
    _Handler.max_active = 0
    _Handler.started = []
    yield


def _assets(base: str) -> list[dict]:
    return [{"name": f"trace.tar.a{chr(97 + i)}", "url": f"{base}/part/{i}",
             "size": len(PARTS[i])} for i in range(len(PARTS))]


def test_serial_download_yields_parts_in_order(archive_server):
    """The serial path (workers = 1) is the reference byte stream."""
    with httpx.Client(timeout=30.0) as client:
        chunks = list(ingest._chunks([a["url"] for a in _assets(archive_server)], client))
    assert b"".join(chunks) == b"".join(PARTS)
    assert _Handler.max_active == 1  # one part at a time


def test_parallel_download_matches_serial_byte_for_byte(archive_server):
    """The parallel path must yield exactly the same bytes, in the same order."""
    with httpx.Client(timeout=30.0) as client:
        serial = b"".join(ingest._chunks([a["url"] for a in _assets(archive_server)], client))
    with httpx.Client(timeout=30.0) as client:
        parallel = b"".join(ingest._parallel_chunks(_assets(archive_server), client, 3))
    assert parallel == serial == b"".join(PARTS)
    assert parallel.startswith(b"alpha-part;") and parallel.endswith(b"gamma-part;")


def test_parallel_download_fetches_parts_concurrently(archive_server):
    """The parts overlap in time — that is the whole point of the change."""
    with httpx.Client(timeout=30.0) as client:
        b"".join(ingest._parallel_chunks(_assets(archive_server), client, 3))
    assert _Handler.max_active > 1, "the parts were not fetched concurrently"
    # The whole parallel pass must not cost three times the part delay.
    span = max(_Handler.started) - min(_Handler.started)
    assert span < PART_DELAY_S * 2, f"the requests did not overlap ({span:.2f}s)"


def test_parallel_download_reports_byte_progress(archive_server):
    """The progress events carry the downloaded bytes and the known total."""
    events: list[dict] = []
    with httpx.Client(timeout=30.0) as client:
        b"".join(ingest._parallel_chunks(_assets(archive_server), client, 3,
                                         on_progress=events.append))
    assert events, "no progress event was published"
    assert all(e["stage"] == "download" for e in events)
    assert events[-1]["done"] == sum(len(p) for p in PARTS)
    assert events[-1]["total"] == sum(len(p) for p in PARTS)
    assert [e["done"] for e in events] == sorted(e["done"] for e in events)


def test_parallel_download_raises_on_failing_part(archive_server):
    """A 404 part must surface as an HTTP error, not as a short stream."""
    with httpx.Client(timeout=30.0) as client:
        with pytest.raises(httpx.HTTPStatusError):
            b"".join(ingest._parallel_chunks(
                [{"name": "x", "url": f"{archive_server}/missing", "size": 10}],
                client, 1))


def test_parallel_download_honours_cancellation(archive_server):
    """A cancelled job must stop the download instead of streaming on."""
    with httpx.Client(timeout=30.0) as client:
        with pytest.raises(RuntimeError, match="cancelled"):
            b"".join(ingest._parallel_chunks(_assets(archive_server), client, 3,
                                             cancelled=lambda: True))


def test_asset_assets_reads_sizes_from_the_release_index(archive_server):
    """``asset_assets`` returns name/url/size; ``asset_urls`` stays a url list."""
    payload = {"assets": [
        {"name": "trace_full.tar.ab", "browser_download_url": "https://example.invalid/b", "size": 20},
        {"name": "trace_full.tar.aa", "browser_download_url": "https://example.invalid/a", "size": 10},
    ]}
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    with httpx.Client(transport=transport) as client:
        import datetime as dt

        assets = ingest.asset_assets(dt.date(2026, 10, 19), client)
        urls = ingest.asset_urls(dt.date(2026, 10, 19), client)
    assert [a["name"] for a in assets] == ["trace_full.tar.aa", "trace_full.tar.ab"]
    assert [a["size"] for a in assets] == [10, 20]
    assert urls == ["https://example.invalid/a", "https://example.invalid/b"]


def test_job_download_uses_the_parallel_path_when_configured(archive_server, monkeypatch):
    """``_download_and_extract`` must pick the parallel downloader.

    The setting decides the path; the archive here is the local server's
    parts, so the wiring is checked end to end (the release index is the
    only stubbed boundary).
    """
    import datetime as dt

    from backend.jobs import _exec_traffic
    from backend.traffic import config as traffic_config

    monkeypatch.setattr(ingest, "asset_assets",
                        lambda day, client: _assets(archive_server))
    monkeypatch.setattr(traffic_config.settings, "ingest_download_workers", 3)
    monkeypatch.setattr(traffic_config.settings, "ingest_download_queue", 2)
    events: list[dict] = []

    def _fake_extract(stream, specs, cancelled=None, **kwargs):
        # Drain the stream so the download really runs.
        total = 0
        while True:
            chunk = stream.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
        return {"LHBP": [{"bytes": total}]}

    monkeypatch.setattr(ingest, "extract_multi", _fake_extract)
    out = _exec_traffic._download_and_extract(
        dt.date(2026, 10, 19), [{"icao": "LHBP"}], lambda: False, events.append)
    assert out == {"LHBP": [{"bytes": sum(len(p) for p in PARTS)}]}
    assert _Handler.max_active > 1, "the job did not use the parallel downloader"
    assert events and events[-1]["stage"] == "download"
    assert events[-1]["done"] == sum(len(p) for p in PARTS)


def test_job_download_stays_serial_without_the_setting(archive_server, monkeypatch):
    """Without the setting the download is serial (one part at a time)."""
    import datetime as dt

    from backend.jobs import _exec_traffic
    from backend.traffic import config as traffic_config

    monkeypatch.setattr(ingest, "asset_assets",
                        lambda day, client: _assets(archive_server))
    monkeypatch.setattr(traffic_config.settings, "ingest_download_workers", 0)
    events: list[dict] = []

    def _fake_extract(stream, specs, cancelled=None, **kwargs):
        while stream.read(1 << 20):
            pass
        return {}

    monkeypatch.setattr(ingest, "extract_multi", _fake_extract)
    _exec_traffic._download_and_extract(
        dt.date(2026, 10, 19), [{"icao": "LHBP"}], lambda: False, events.append)
    assert _Handler.max_active == 1, "the serial path fetched the parts concurrently"
    assert events and events[-1]["stage"] == "download"
