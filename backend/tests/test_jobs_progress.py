"""Job store progress semantics (issue #66: the ingest progress counters).

The job document is replaced as a whole, so the progress writer must never
resurrect a job that was cancelled or finished meanwhile — a lost cancel
would leave a 4 GB download running with nobody able to stop it.

* ``_progress_doc`` — the pure decision (pure algorithm, no I/O).
* ``set_progress`` against the in-process fallback (no Redis): the
  cancelling/terminal document is left alone.
* ``set_progress`` against a **real Redis** (the compare-and-set path):
  the progress is written, a cancelling document is skipped, and a
  concurrent write between the read and the write is not clobbered.

The Redis test needs a server, so it runs only when ``REDIS_TEST_URL`` is
set (e.g. ``redis://127.0.0.1:6379/0``); without it the test is skipped
with that reason, and the hermetic fallback covers the same decisions.

Manual / ``@live`` follow-up: the compare-and-set path against the
production Redis (the dev container's ``fundvista-redis``), which the
skipped test covers when ``REDIS_TEST_URL`` points at it.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from backend.jobs import store as jobs_store


def _doc(status: str = "running") -> dict:
    return {"job_id": "x" * 32, "type": "traffic_ingest", "status": status,
            "progress": {"stage": "queued", "done": 0, "total": 0},
            "result": None, "error": None}


PROGRESS = {"stage": "download", "done": 100, "total": 300}


# -- the pure decision -------------------------------------------------------

def test_progress_doc_applies_the_counters():
    out = jobs_store._progress_doc(_doc("running"), PROGRESS)
    assert out is not None
    assert out["progress"] == PROGRESS
    assert out["status"] == "running"


def test_progress_doc_skips_cancelling_and_terminal():
    for status in ("cancelling", "done", "failed", "cancelled"):
        assert jobs_store._progress_doc(_doc(status), PROGRESS) is None, status


def test_progress_doc_skips_a_missing_job():
    assert jobs_store._progress_doc(None, PROGRESS) is None


def test_progress_doc_does_not_mutate_the_input():
    doc = _doc("running")
    jobs_store._progress_doc(doc, PROGRESS)
    assert doc["progress"] == {"stage": "queued", "done": 0, "total": 0}


# -- the in-process fallback -------------------------------------------------

def test_set_progress_updates_the_document():
    async def go():
        jobs_store.reset_memory()
        jobs_store.configure_jobs_redis(None)
        await jobs_store.create_job("j" * 32, "traffic_ingest", {"icao": "LHBP"}, "1.2.3.4")
        await jobs_store.set_progress("j" * 32, "download", 100, 300)
        return await jobs_store.get_job("j" * 32)

    doc = asyncio.run(go())
    assert doc is not None
    assert doc["progress"] == {"stage": "download", "done": 100, "total": 300}


def test_set_progress_never_resurrects_a_cancelled_job():
    async def go():
        jobs_store.reset_memory()
        jobs_store.configure_jobs_redis(None)
        await jobs_store.create_job("j" * 32, "traffic_ingest", {"icao": "LHBP"}, "1.2.3.4")
        await jobs_store.request_cancel("j" * 32)
        await jobs_store.set_progress("j" * 32, "download", 100, 300)
        return await jobs_store.get_job("j" * 32)

    doc = asyncio.run(go())
    assert doc is not None
    assert doc["status"] == "cancelling", doc
    assert doc["progress"]["stage"] != "download", doc


# -- the real-Redis compare-and-set -----------------------------------------

REDIS_URL = os.environ.get("REDIS_TEST_URL")


class _RacingPipeline:
    """Pipeline proxy that writes the key right after the CAS read it."""

    def __init__(self, pipe, client, key, racing_doc: dict) -> None:
        self._pipe = pipe
        self._client = client
        self._key = key
        self._racing_doc = racing_doc

    async def __aenter__(self):
        await self._pipe.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._pipe.__aexit__(*exc)

    async def watch(self, key):
        return await self._pipe.watch(key)

    async def get(self, key):
        value = await self._pipe.get(key)
        await self._client.setex(self._key, 60, json.dumps(self._racing_doc))
        return value

    async def unwatch(self):
        return await self._pipe.unwatch()

    def multi(self):
        return self._pipe.multi()

    def setex(self, *args, **kwargs):
        return self._pipe.setex(*args, **kwargs)

    async def execute(self):
        return await self._pipe.execute()


class _RacingClient:
    """Redis proxy whose pipeline injects a concurrent write."""

    def __init__(self, client, key, racing_doc: dict) -> None:
        self._client = client
        self._key = key
        self._racing_doc = racing_doc

    def pipeline(self, **kwargs):
        return _RacingPipeline(self._client.pipeline(**kwargs), self._client,
                               self._key, self._racing_doc)

    def __getattr__(self, name):
        return getattr(self._client, name)


@pytest.mark.skipif(not REDIS_URL, reason="REDIS_TEST_URL is not set (no Redis for the CAS path)")
def test_set_progress_redis_compare_and_set():
    """The progress write must not clobber a concurrent status change."""
    from redis.asyncio import Redis

    async def go():
        client = Redis.from_url(REDIS_URL, decode_responses=False)
        jobs_store.reset_memory()
        jobs_store.configure_jobs_redis(client)
        job_id = "j" * 32
        key = f"job:{job_id}"
        await client.delete(key)
        await jobs_store.create_job(job_id, "traffic_ingest", {"icao": "LHBP"}, "1.2.3.4")
        try:
            # 1) a normal progress write lands
            await jobs_store.set_progress(job_id, "download", 100, 300)
            doc = json.loads(await client.get(key))
            assert doc["progress"] == {"stage": "download", "done": 100, "total": 300}

            # 2) a cancelling job is not written to
            await jobs_store.request_cancel(job_id)
            await jobs_store.set_progress(job_id, "parse", 5, 0)
            doc = json.loads(await client.get(key))
            assert doc["status"] == "cancelling", doc
            assert doc["progress"]["stage"] == "download", doc

            # 3) a concurrent write between the read and the write survives:
            #    the CAS must abort, re-read and skip the cancelling job.
            await client.setex(key, 60, json.dumps(_doc("running")))
            racing = _RacingClient(client, key, _doc("cancelling"))
            jobs_store.configure_jobs_redis(racing)
            await jobs_store.set_progress(job_id, "download", 999, 300)
            jobs_store.configure_jobs_redis(client)
            doc = json.loads(await client.get(key))
            assert doc["status"] == "cancelling", doc
            assert doc["progress"]["done"] != 999, doc
        finally:
            await client.delete(key)
            jobs_store.configure_jobs_redis(None)
            await client.aclose()

    asyncio.run(go())
