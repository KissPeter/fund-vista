"""Issue #1061: a restart must turn lost `running` jobs terminal.

Contract (pytest + the in-memory store; the Redis path shares every rule):

* The runner claims the doc (`runner` stamp) and heartbeats its liveness —
  a claim with a LIVE runner survives the sweep no matter how old
  `updated_at` is (the download stage is silent for minutes by design).
* A non-terminal doc whose runner is NOT alive (worker/container died) and
  older than the claim grace is reaped: status `failed`,
  `error.code == "orphaned"`.
* A doc younger than the claim grace is never touched (the enqueue→claim
  window is single-digit milliseconds).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.jobs import store as jobs_store


@pytest.fixture(autouse=True)
def _fresh_store():
    jobs_store.reset_memory()
    jobs_store.configure_jobs_redis(None)
    yield
    jobs_store.reset_memory()


def _make_job(job_id: str, status: str = "running") -> None:
    async def _do() -> None:
        await jobs_store.create_job(job_id, "traffic_ingest", {}, "10.0.0.1")
        if status == "running":
            await jobs_store.set_status(
                job_id, "running", progress={"stage": "download"}
            )
    asyncio.run(_do())


def _age_doc(job_id: str, age_s: float) -> None:
    entry = jobs_store._memory[f"job:{job_id}"]
    entry[1]["updated_at"] = time.time() - age_s


def test_live_runner_survives_despite_old_scan_age():
    # The runner claims + heartbeats: `runner` present and alive.
    _make_job("orphan-1", "running")

    async def _do() -> None:
        await jobs_store.claim_job("orphan-1", "worker-a")
        await jobs_store.touch_runner("worker-a")
        reaped = await jobs_store.reap_orphans()
        assert reaped == []
        doc = await jobs_store.get_job("orphan-1")
        assert doc is not None and doc["status"] == "running"
        # Age the doc well past the grace; the LIVE runner keeps it.
        entry = jobs_store._memory["job:orphan-1"]
        entry[1]["updated_at"] = time.time() - 600.0
        assert await jobs_store.reap_orphans() == []

    asyncio.run(_do())


def test_dead_runner_is_reaped():
    _make_job("orphan-2", "running")

    async def _do() -> None:
        await jobs_store.claim_job("orphan-2", "worker-b")
        # The worker died: no more heartbeats → the liveness deadline lapses.
        jobs_store.runner_deadline_set("worker-b", time.time() - 1.0)
        entry = jobs_store._memory["job:orphan-2"]
        entry[1]["updated_at"] = time.time() - 60.0
        reaped = await jobs_store.reap_orphans()
        assert reaped == ["orphan-2"]
        doc = await jobs_store.get_job("orphan-2")
        assert doc is not None
        assert doc["status"] == "failed"
        assert doc["error"]["code"] == "orphaned"
        # The failed doc leaves the singleflight set (the terminal rule).
        members = await jobs_store.list_nonterminal_by_ip_type(
            "10.0.0.1", "traffic_ingest"
        )
        assert "orphan-2" not in members

    asyncio.run(_do())


def test_fresh_doc_is_never_reaped():
    _make_job("orphan-3", "queued")

    async def _do() -> None:
        # No claim yet, but created seconds ago: inside the enqueue→claim
        # grace window.
        reaped = await jobs_store.reap_orphans()
        assert reaped == []
        doc = await jobs_store.get_job("orphan-3")
        assert doc is not None and doc["status"] == "queued"

    asyncio.run(_do())


def test_aged_queued_doc_without_runner_is_reaped():
    _make_job("orphan-4", "queued")

    async def _do() -> None:
        # A queued doc whose worker died before the claim (or whose stamp was
        # never written) is also unreachable: reap it once aged.
        entry = jobs_store._memory["job:orphan-4"]
        entry[1]["updated_at"] = time.time() - 300.0
        reaped = await jobs_store.reap_orphans()
        assert reaped == ["orphan-4"]
        doc = await jobs_store.get_job("orphan-4")
        assert doc is not None
        assert doc["status"] == "failed"
        assert doc["error"]["code"] == "orphaned"

    asyncio.run(_do())


def test_reap_never_touches_terminal_docs():
    _make_job("orphan-5", "running")

    async def _do() -> None:
        entry = jobs_store._memory["job:orphan-5"]
        entry[1]["updated_at"] = time.time() - 900.0
        await jobs_store.set_cancelled("orphan-5")
        entry[1]["updated_at"] = time.time() - 900.0
        reaped = await jobs_store.reap_orphans()
        assert reaped == []
        doc = await jobs_store.get_job("orphan-5")
        assert doc is not None and doc["status"] == "cancelled"

    asyncio.run(_do())
