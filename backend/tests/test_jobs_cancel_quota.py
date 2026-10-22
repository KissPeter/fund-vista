"""Job cancel-while-queued: the per-IP job quota must not be burned (issue #92).

Live-HTTP integration (in-process uvicorn on a socket - never TestClient, per
the test-design skill), hermetic: Redis -> memory fallback, rate limit wide
open, and the runner's ``enqueue`` stubbed so the created jobs stay in the
``queued`` stage (the window a DELETE has to win against).

What is covered:

* ``DELETE /v1/jobs/{id}`` on a job that never started writes a **terminal**
  server-side status (``cancelled``), not the non-terminal ``cancelling``
  (the GET projection hides the difference - the store is the truth).
* Cancelling 3 queued jobs does not trip the per-IP quota: a 4th
  ``POST /v1/jobs`` is still accepted (202).
* Negative/boundary: 3 *live* (non-cancelled) queued jobs do trip the quota -
  the 4th POST is rejected with 429 ``rate_limited``.

Manual / ``@live`` follow-ups (cannot run hermetically): the multi-worker
cross-process cancel path (a job queued on another worker) - the same store
transitions are covered here through the memory fallback.
"""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import threading
import time

os.environ.setdefault("PENPLOT_DATA_DIR", tempfile.mkdtemp(prefix="penplot-jobs-cancel-"))
os.environ["REDIS_CLOUD_URL"] = "redis://127.0.0.1:1/0"
os.environ["PENPLOT_RATE_LIMIT"] = "100000"

import httpx
import pytest
import uvicorn

from backend.jobs import runner as jobs_runner
from backend.jobs import store as jobs_store

JOB_BODY = {
    "type": "citymap_render",
    "request": {
        "bbox": {
            "south": 47.4500,
            "west": 19.0200,
            "north": 47.5500,
            "east": 19.1500,
        },
        "layers": ["roads", "buildings"],
        "min_path_len_m": 10.0,
        "width": 1000,
    },
    "cancel_previous": False,
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def live():
    """Real HTTP: in-process uvicorn on a socket (module scope, one server)."""
    from backend.main import app as full_app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        full_app, host="127.0.0.1", port=port,
        log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30.0
    with httpx.Client(timeout=2.0) as probe:
        while time.time() < deadline:
            try:
                if probe.get(f"{base}/healthz").status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("jobs cancel test server never became healthy")
    with httpx.Client(base_url=base, timeout=30.0) as c:
        yield c
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Per-test state reset + a runner that never picks the job up."""
    jobs_store.reset_memory()
    jobs_runner.reset_local()
    jobs_store.configure_jobs_redis(None)
    from backend.penplot import router as penplot_router

    penplot_router._limiter._memory.clear()
    # The job stays in the 'queued' stage (the window the DELETE must win).
    monkeypatch.setattr(jobs_runner, "enqueue", lambda job_id: None)
    yield
    jobs_runner.reset_local()
    jobs_store.reset_memory()


def _post_job(c: httpx.Client) -> httpx.Response:
    return c.post("/v1/jobs", json=JOB_BODY)


def _doc(job_id: str) -> dict:
    """The server-side job document (the GET projection hides 'cancelling')."""
    doc = asyncio.run(jobs_store.get_job(job_id))
    assert doc is not None, f"job {job_id} vanished from the store"
    return doc


def test_queued_cancel_reaches_a_terminal_status(live):
    resp = _post_job(live)
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    assert _doc(job_id)["status"] == "queued"

    dele = live.delete(f"/v1/jobs/{job_id}")
    assert dele.status_code == 200, dele.text
    assert dele.json() == {"job_id": job_id, "status": "cancelled"}

    # Server-side the job must be terminal, not the transient 'cancelling'.
    assert _doc(job_id)["status"] == "cancelled"
    assert live.get(f"/v1/jobs/{job_id}").json()["status"] == "cancelled"


def test_three_cancelled_queued_jobs_do_not_trip_the_per_ip_quota(live):
    ids = []
    for _ in range(3):
        resp = _post_job(live)
        assert resp.status_code == 202, resp.text
        ids.append(resp.json()["job_id"])
    for job_id in ids:
        assert live.delete(f"/v1/jobs/{job_id}").status_code == 200
        assert _doc(job_id)["status"] == "cancelled"

    # The quota is free again: the 4th job is accepted.
    resp = _post_job(live)
    assert resp.status_code == 202, resp.text


def test_three_live_queued_jobs_trip_the_per_ip_quota(live):
    """Negative/boundary: the cap still holds for jobs that are not cancelled."""
    for _ in range(3):
        assert _post_job(live).status_code == 202

    resp = _post_job(live)
    assert resp.status_code == 429, resp.text
    assert resp.json()["error"]["code"] == "rate_limited"
