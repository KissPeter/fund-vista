"""Live-server fixtures: REAL HTTP over a socket, no FastAPI TestClient.

A separate uvicorn *subprocess* is spawned per test session on an ephemeral
port and driven with httpx over 127.0.0.1. This exercises the full stack —
ASGI server, multipart parsing, routing order vs. the catch-all proxy,
error handlers — exactly as production serves it.

Point PENPLOT_DATA_DIR at a tmp dir so tests never touch real data.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(base_url: str, proc: subprocess.Popen, timeout_s: float = 30.0) -> None:
    deadline = time.time() + timeout_s
    last_err = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"uvicorn exited early (code={proc.returncode}). stderr:\n"
                f"{(proc.stderr.read() if proc.stderr else '')[-4000:]}"
            )
        try:
            resp = httpx.get(f"{base_url}/healthz", timeout=2.0)
            if resp.status_code == 200:
                return
        except Exception as exc:  # server not up yet
            last_err = str(exc)
        time.sleep(0.25)
    raise RuntimeError(f"uvicorn never became healthy: {last_err}")


@pytest.fixture(scope="session")
def live_server(tmp_path_factory: pytest.TempPathFactory):
    """Yield the base URL of a real uvicorn subprocess."""
    data_dir = str(tmp_path_factory.mktemp("penplot-data"))
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env["PENPLOT_DATA_DIR"] = data_dir
    env["LOG_LEVEL"] = "WARNING"
    # Hermetic: never touch a developer's real Redis, and force the penplot
    # rate limiter onto its in-process fallback so windows can't leak between
    # test runs.
    env["REDIS_CLOUD_URL"] = "redis://127.0.0.1:1/0"
    # The full suite makes well under 100k requests/min; force the limiter
    # off so tests never trip 429 accidentally (see test_penplot_ratelimit.py
    # for a dedicated low-limit server exercising the 429 path).
    env["PENPLOT_RATE_LIMIT"] = "100000"
    # The subprocess must import the same repo checkout.
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_health(base_url, proc)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def http_client(live_server: str):
    """Plain httpx client pointed at the live server (60s for flow method)."""
    with httpx.Client(base_url=live_server, timeout=60.0) as client:
        yield client
