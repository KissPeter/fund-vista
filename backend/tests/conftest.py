"""Live-server fixtures: REAL HTTP over a socket, no FastAPI TestClient.

A separate uvicorn *subprocess* is spawned per test session on an ephemeral
port and driven with httpx over 127.0.0.1. This exercises the full stack -
ASGI server, multipart parsing, routing order vs. the catch-all proxy,
error handlers - exactly as production serves it.

Point PENPLOT_DATA_DIR at a tmp dir so tests never touch real data.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

# The default test instance is locked (PENPLOT_ALLOW_SVG unset). Only the
# explicit internal_* fixtures below start a subprocess with allow_svg on.

import httpx
import pytest

from backend.tests.helpers import TEST_HMAC_SECRET

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
    # Still alive but not serving: kill it so its stderr can be read, and
    # report that instead of the bare "Connection refused".
    proc.kill()
    proc.wait(timeout=10)
    stderr_tail = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace")[-4000:]
    raise RuntimeError(
        f"uvicorn never became healthy: {last_err}; process alive for "
        f"{timeout_s:.0f}s without serving. stderr:\n{stderr_tail}"
    )


def _start_live_server(tmp_path_factory: pytest.TempPathFactory, name: str, *, allow_svg: bool):
    """Start one hermetic uvicorn subprocess; public and internal differ by env."""
    data_dir = str(tmp_path_factory.mktemp(name))
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env["PENPLOT_DATA_DIR"] = data_dir
    env["LOG_LEVEL"] = "WARNING"
    # Phase 6: inherited env MUST NOT turn a public test server into an SVG one.
    env["PENPLOT_ALLOW_SVG"] = "1" if allow_svg else "0"
    # Hermetic: both loopback twins MUST use the fixture upstream, regardless
    # of which test first creates the session-scoped client.
    env["FUNDVISTA_UPSTREAM_MODE"] = "mock"
    # Hermetic: never touch a developer's real Redis, and force the penplot
    # rate limiter onto its in-process fallback so windows can't leak between
    # test runs.
    env["REDIS_CLOUD_URL"] = "redis://127.0.0.1:1/0"
    # Rate limiting genuinely off (affilio's DISABLE_RATELIMIT switch) so
    # tests never trip 429 accidentally (see test_penplot_ratelimit.py
    # for a dedicated low-limit server exercising the 429 path).
    env["DISABLE_RATELIMIT"] = "True"
    # Shop bridge signing key so token endpoints exercise the real HMAC path
    # (see backend/tests/helpers.py TEST_HMAC_SECRET).
    env["PENPIXEL_HMAC_SECRET"] = TEST_HMAC_SECRET
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
def live_server(tmp_path_factory: pytest.TempPathFactory):
    """Yield the loopback-only, SVG-capable test server used by vector tests."""
    yield from _start_live_server(tmp_path_factory, "penplot-internal", allow_svg=True)


@pytest.fixture(scope="session")
def public_live_server(tmp_path_factory: pytest.TempPathFactory):
    """Yield the public, browser-reachable test server (PNG only)."""
    yield from _start_live_server(tmp_path_factory, "penplot-public", allow_svg=False)


@pytest.fixture(scope="session")
def http_client(live_server: str):
    """Client pointed at the internal server (60s for flow method)."""
    with httpx.Client(base_url=live_server, timeout=60.0) as client:
        yield client


@pytest.fixture(scope="session")
def internal_http_client(http_client: httpx.Client):
    """Explicit alias for tests that exercise the SVG-capable server."""
    yield http_client


@pytest.fixture(scope="session")
def public_http_client(public_live_server: str):
    """Client pointed at the browser-reachable server; MUST see PNG only."""
    with httpx.Client(base_url=public_live_server, timeout=60.0) as client:
        yield client


@pytest.fixture(autouse=True)
def _reset_overpass_mirror_parks():
    """A 429/5xx parks a mirror process-wide; never leak that across tests."""
    from backend.http.mirror_health import overpass_mirrors

    overpass_mirrors.reset()
    yield
    overpass_mirrors.reset()


@pytest.fixture(autouse=True)
def _internal_svg_capable_services(request, monkeypatch):
    """Keep in-process tests on the internal SVG-capable settings.

    The public HTTP fixtures start their own locked subprocess and MUST NOT be
    affected. No environment variable is set here, so a bare ``Settings()``
    still observes the locked production default.
    """
    if any(
        name in request.fixturenames
        for name in ("public_http_client", "public_live_server")
    ):
        return

    from backend.fulfillment import router as fulfillment_router
    from backend.pcb import router as pcb_router
    from backend.penplot import router as penplot_router

    for module in (penplot_router, fulfillment_router, pcb_router):
        monkeypatch.setattr(
            module,
            "_settings",
            module._settings.model_copy(update={"allow_svg": True}),
        )


def pytest_addoption(parser):
    parser.addoption(
        "--run-perf", action="store_true", default=False,
        help="Run performance-budget tests (skipped by default on the shared CI runner).",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "perf: performance-budget tests (run with --run-perf)")
    config.addinivalue_line(
        "markers", "network: live call to an external store; skipped unless selected with -m network"
    )


def pytest_collection_modifyitems(config, items):
    """Skip perf budgets unless --run-perf, and network tests unless -m network."""
    if not config.getoption("--run-perf"):
        skip_perf = pytest.mark.skip(reason="performance-budget test; run with --run-perf")
        for item in items:
            if "perf" in item.keywords:
                item.add_marker(skip_perf)
    if "network" not in (config.getoption("-m") or ""):
        skip_network = pytest.mark.skip(reason="live network test; run with -m network")
        for item in items:
            if "network" in item.keywords:
                item.add_marker(skip_network)
