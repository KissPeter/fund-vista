"""Orphaned job reaping (issue #1061).

The runner task, its cancel poller and the runtime watchdog live in the
memory of ONE uvicorn worker. A worker or container restart loses all
three: the Redis doc stays ``running`` until its TTL expires, then the poll
answers ``404 job_not_found`` — the client sees a vanished job instead of a
failed one.

The liveness key (``job:live:{id}``) is what tells the two apart: the
owning worker refreshes it while the task runs, and it expires when the
owner is gone. A poll reaps a job whose liveness key is gone and whose doc
shows no activity for ``ORPHAN_GRACE_S``.

Contract (pytest + the real app, the process-local job store):

* a stale ``running`` doc without a liveness key → ``GET`` → ``200 failed``
  with ``error.code == "orphaned"`` (never a 404).
* the same doc WITH a live liveness key → ``GET`` → still ``running`` (a
  healthy long job is never failed).
* a fresh doc without a liveness key → ``GET`` → still ``queued`` (the
  grace window: the owner may not have started the task yet).
* a terminal doc → ``GET`` → unchanged.
* the runner refreshes the liveness key while the task runs.
* a reaped job frees its per-IP non-terminal slot.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time

os.environ.setdefault("PENPLOT_DATA_DIR", tempfile.mkdtemp(prefix="penplot-orphan-"))
os.environ["REDIS_CLOUD_URL"] = "redis://127.0.0.1:1/0"
os.environ["PENPLOT_RATE_LIMIT"] = "100000"

import pytest
from fastapi.testclient import TestClient

from backend.jobs import runner as jobs_runner
from backend.jobs import store as jobs_store
from backend.jobs.schemas import ORPHAN_GRACE_S
from backend.main import app as full_app

JOB_ID = "a" * 32


@pytest.fixture()
def client():
    jobs_store.reset_memory()
    jobs_runner.reset_local()
    jobs_store.configure_jobs_redis(None)
    from backend.penplot import router as penplot_router

    penplot_router._limiter._memory.clear()
    with TestClient(full_app) as c:
        yield c
    jobs_runner.reset_local()
    jobs_store.reset_memory()


def _make_job(status: str = "running", ip: str = "10.0.0.1") -> str:
    """Create a job doc in the store (no runner, no network)."""
    asyncio.run(jobs_store.create_job(JOB_ID, "convert", {"image_id": "x"}, ip))
    _set_status(status)
    return JOB_ID


def _set_status(status: str) -> None:
    exp, doc = jobs_store._memory[jobs_store._job_key(JOB_ID)]
    doc["status"] = status
    jobs_store._memory[jobs_store._job_key(JOB_ID)] = (exp, doc)


def _age_doc(seconds: float) -> None:
    """Backdate the doc's updated_at (the owner died mid-job)."""
    exp, doc = jobs_store._memory[jobs_store._job_key(JOB_ID)]
    doc["updated_at"] = time.time() - seconds
    jobs_store._memory[jobs_store._job_key(JOB_ID)] = (exp, doc)


def _heartbeat() -> None:
    asyncio.run(jobs_store.heartbeat(JOB_ID))


def test_stale_running_job_without_liveness_is_failed_as_orphaned(client):
    _make_job("running")
    _age_doc(ORPHAN_GRACE_S + 60)

    resp = client.get(f"/v1/jobs/{JOB_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed", body
    assert body["error"]["code"] == "orphaned", body
    assert body["result"] is None


def test_live_job_is_never_reaped(client):
    _make_job("running")
    _age_doc(ORPHAN_GRACE_S + 60)
    _heartbeat()  # the owner is alive

    resp = client.get(f"/v1/jobs/{JOB_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "running", body
    assert body["error"] is None


def test_fresh_job_without_liveness_is_not_reaped(client):
    """The owner may not have started the task yet — the grace protects it."""
    _make_job("queued")

    resp = client.get(f"/v1/jobs/{JOB_ID}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "queued"


def test_terminal_job_is_not_reaped(client):
    _make_job("done")
    _age_doc(ORPHAN_GRACE_S + 60)

    resp = client.get(f"/v1/jobs/{JOB_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "done", body
    assert body["error"] is None


def test_stale_cancelling_job_becomes_cancelled(client):
    """The client asked to cancel and nobody carried it out."""
    _make_job("cancelling")
    _age_doc(ORPHAN_GRACE_S + 60)

    resp = client.get(f"/v1/jobs/{JOB_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "cancelled", body


def test_reaped_job_frees_the_per_ip_slot(client):
    _make_job("running", ip="10.0.0.9")
    _age_doc(ORPHAN_GRACE_S + 60)
    assert asyncio.run(jobs_store.count_nonterminal_by_ip("10.0.0.9")) == 1

    resp = client.get(f"/v1/jobs/{JOB_ID}")
    assert resp.status_code == 200, resp.text
    assert asyncio.run(jobs_store.count_nonterminal_by_ip("10.0.0.9")) == 0


def test_runner_keeps_the_liveness_key_alive(client, monkeypatch):
    """A running job owns a liveness key — the reaper's input."""
    from backend.tests.helpers import default_params, png_bytes, upload

    up = upload(client, png_bytes(64, 64))
    assert up.status_code == 200, up.text
    image_id = up.json()["image_id"]

    started = threading.Event()
    release = threading.Event()

    async def blocking_exec_convert(payload, job_id, stop, cancelled):
        started.set()
        # Yield to the loop: the heartbeat task must get a turn (a blocking
        # wait here would starve it, exactly like a blocked event loop).
        while not release.is_set():
            await asyncio.sleep(0.05)
        return {
            "image_id": payload["image_id"],
            "svg_url": "http://example.test/result.svg",
            "vpype_command": "vpype",
            "stats": {},
            "warnings": [],
        }

    monkeypatch.setattr(jobs_runner, "_exec_convert", blocking_exec_convert)

    resp = client.post(
        "/v1/jobs",
        json={
            "type": "convert",
            "request": {"image_id": image_id, "params": default_params("hatch")},
            "cancel_previous": False,
        },
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    assert started.wait(timeout=15), "job never started running"

    deadline = time.time() + 5.0
    while time.time() < deadline and not asyncio.run(jobs_store.is_alive(job_id)):
        time.sleep(0.05)
    assert asyncio.run(jobs_store.is_alive(job_id)) is True, (
        "the running job owns no liveness key"
    )

    release.set()
