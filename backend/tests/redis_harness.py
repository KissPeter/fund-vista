"""A real, throwaway redis-server for the API integration tests.

The live-server suites run the app in-process and assert on what actually
lands in Redis (key prefix + decoded value), not just on ``cache_hit``
flags. An unreachable Redis would silently degrade to the in-process
fallback and make those assertions meaningless, so this harness never
hands out a dead URL: it starts ``redis-server`` on a free loopback port,
owned by the test process, and tears it down at exit.

Needs ``redis-server`` on PATH (``brew install redis`` locally,
``apt-get install redis-server`` on the CI runner).
"""

from __future__ import annotations

import atexit
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager

import redis

from backend.cachelib import decode_value

_proc: subprocess.Popen | None = None
_url: str | None = None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stop() -> None:
    if _proc is not None and _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _proc.kill()


def harness_redis_url() -> str:
    """URL of the harness Redis, started on first use."""
    global _proc, _url
    if _url is not None:
        return _url
    binary = shutil.which("redis-server")
    if binary is None:
        raise RuntimeError(
            "redis-server not on PATH: the API tests assert Redis contents "
            "(brew install redis / apt-get install redis-server)"
        )
    port = _free_port()
    data_dir = tempfile.mkdtemp(prefix="fv-test-redis-")
    _proc = subprocess.Popen(
        [binary, "--port", str(port), "--bind", "127.0.0.1",
         "--save", "", "--appendonly", "no", "--dir", data_dir],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    atexit.register(_stop)
    _url = f"redis://127.0.0.1:{port}/0"
    deadline = time.time() + 10.0
    while time.time() < deadline:
        try:
            redis.Redis.from_url(_url).ping()
            return _url
        except redis.ConnectionError:
            time.sleep(0.05)
    raise RuntimeError(f"test redis-server never answered on {_url}")


@contextmanager
def app_on_harness_redis() -> Iterator[None]:
    """Point the in-process app's lifespan at the harness Redis.

    ``backend.main`` reads ``REDIS_CLOUD_URL`` once, at first import, and many
    test modules set it to a dead port at import time, so the environment
    cannot be relied on. The lifespan reads the module-level ``settings`` at
    startup, so swap that for the duration of the server's life.
    """
    from _pytest.monkeypatch import MonkeyPatch

    from backend import main as backend_main

    with MonkeyPatch.context() as mp:
        mp.setattr(
            backend_main,
            "settings",
            backend_main.settings.model_copy(update={"redis_cloud_url": harness_redis_url()}),
        )
        yield


def client() -> redis.Redis:
    return redis.Redis.from_url(harness_redis_url(), decode_responses=False)


def flush() -> None:
    """Cold cache for the next test (the harness DB holds nothing else)."""
    client().flushdb()


def cached_values(prefix: str) -> dict[str, str]:
    """Every key under ``prefix`` mapped to its decoded cached value."""
    r = client()
    out: dict[str, str] = {}
    for raw_key in r.scan_iter(match=f"{prefix}*"):
        raw = r.get(raw_key)
        if raw is not None:
            out[raw_key.decode("utf-8")] = decode_value(raw)
    return out
