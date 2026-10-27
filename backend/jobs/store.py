"""Redis-backed job state with an in-process fallback (plan P3.2).

Redis is the source of truth across the 4 uvicorn workers:

* ``job:{id}`` — JSON doc
  ``{job_id,type,request,status,progress,result,error,client_ip,
  created_at,updated_at}`` with a TTL (results 1 h, failures 10 min,
  cancelled/queued/running 1 h).
* ``jobs:pending`` — LPUSH on enqueue (dispatchers BRPOP; this worker also
  runs its own enqueues directly so single-process deploys need no loop).
* ``ip:{ip}:{type}`` — Redis set of non-terminal job ids for
  ``cancel_previous`` singleflight (TTL 10 min).
* ``job:cancel:{id}`` — PUBLISH on cancel (wake-up; runners also poll the
  status every 200 ms so a missed pubsub message still cancels).

Without Redis (tests, degraded mode) the same API runs on a process-local
dict with per-key expiry so contract tests stay hermetic. All Redis errors
degrade to a warning + miss/False rather than failing the request.
"""

from __future__ import annotations

import json
import logging
import time

from backend.jobs.schemas import (
    CANCELLED_TTL_S,
    FAILURE_TTL_S,
    QUEUED_TTL_S,
    RESULT_TTL_S,
    SUPERSEDE_SET_TTL_S,
    JobType,
)

log = logging.getLogger(__name__)

TERMINAL = ("done", "failed", "cancelled")

_redis = None
_memory: dict[str, tuple[float, dict]] = {}
_ip_sets: dict[str, tuple[float, set[str]]] = {}
_pending: list[str] = []
# Runner liveness (issue #1061): `runner:{worker_id}` in Redis with a short
# TTL, refreshed by the runner's heartbeat; the in-memory fallback keeps
# expiring deadlines. A non-terminal job doc whose runner is NOT alive is an
# orphan — the owning worker/container died and nothing will reach a
# terminal state for it (the shop's poller would sit on `running` until the
# queued TTL expires, then read a raw 404).
_runner_live: dict[str, float] = {}

RUNNER_TTL_S = 30
# A job doc may sit between create_job and the runner's claim for the few
# milliseconds of the enqueue; anything older than this grace without a live
# runner is orphaned.
CLAIM_GRACE_S = 10.0


def configure_jobs_redis(client) -> None:
    """Inject the shared Redis client (None = memory fallback)."""
    global _redis
    _redis = client
    _memory.clear()
    _ip_sets.clear()
    _pending.clear()
    _runner_live.clear()


def runner_deadline_set(worker_id: str, deadline: float) -> None:
    """Test helper: pre-age a runner's liveness deadline."""
    _runner_live[worker_id] = deadline


def _job_key(job_id: str) -> str:
    return f"job:{job_id}"


def _ip_key(ip: str, job_type: str) -> str:
    return f"ip:{ip}:{job_type}"


def _prune(now: float) -> None:
    for key in [k for k, (exp, _) in _memory.items() if exp <= now]:
        del _memory[key]
    for key in [k for k, (exp, _) in _ip_sets.items() if exp <= now]:
        del _ip_sets[key]


def _ttl_for(status: str) -> int:
    if status == "done":
        return RESULT_TTL_S
    if status == "failed":
        return FAILURE_TTL_S
    if status == "cancelled":
        return CANCELLED_TTL_S
    return QUEUED_TTL_S


async def create_job(
    job_id: str, job_type: JobType, payload: dict, client_ip: str,
    superseded: list[str] | None = None,
) -> None:
    now = time.time()
    doc = {
        "job_id": job_id,
        "type": job_type,
        "request": payload,
        "status": "queued",
        "progress": {"stage": "queued", "done": 0, "total": 0},
        "result": None,
        "error": None,
        "client_ip": client_ip,
        "superseded": superseded or [],
        "created_at": now,
        "updated_at": now,
    }
    if _redis is not None:
        try:
            await _redis.setex(_job_key(job_id), QUEUED_TTL_S, json.dumps(doc))
            try:
                await _redis.lpush("jobs:pending", job_id)
                await _redis.sadd(_ip_key(client_ip, job_type), job_id)
                await _redis.expire(_ip_key(client_ip, job_type), SUPERSEDE_SET_TTL_S)
            except Exception as exc:
                log.warning("jobs.redis index write failed id=%s: %s", job_id[:12], exc)
            return
        except Exception as exc:
            log.warning("jobs.redis write failed id=%s: %s", job_id[:12], exc)
    _prune(now)
    _memory[_job_key(job_id)] = (now + QUEUED_TTL_S, doc)
    key = _ip_key(client_ip, job_type)
    _, members = _ip_sets.get(key, (0.0, set()))
    members = set(members)
    members.add(job_id)
    _ip_sets[key] = (now + SUPERSEDE_SET_TTL_S, members)
    _pending.append(job_id)


async def get_job(job_id: str) -> dict | None:
    if _redis is not None:
        try:
            raw = await _redis.get(_job_key(job_id))
            if raw is None:
                # Memory may hold a job this worker created before Redis came up.
                doc = _memory.get(_job_key(job_id))
                if doc is not None and doc[0] > time.time():
                    return doc[1]
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return json.loads(raw)
        except Exception as exc:
            log.warning("jobs.redis read failed id=%s: %s", job_id[:12], exc)
    _prune(time.time())
    entry = _memory.get(_job_key(job_id))
    return entry[1] if entry is not None else None


async def _write_doc(job_id: str, doc: dict, ttl_s: int) -> None:
    doc["updated_at"] = time.time()
    if _redis is not None:
        try:
            await _redis.setex(_job_key(job_id), ttl_s, json.dumps(doc))
        except Exception as exc:
            log.warning("jobs.redis write failed id=%s: %s", job_id[:12], exc)
    now = time.time()
    _prune(now)
    _memory[_job_key(job_id)] = (now + ttl_s, doc)


async def _remove_from_ip_sets(job_id: str, doc: dict) -> None:
    ip, typ = doc.get("client_ip", ""), doc.get("type", "")
    if not ip or not typ:
        return
    key = _ip_key(ip, typ)
    if _redis is not None:
        try:
            await _redis.srem(key, job_id)
        except Exception:
            pass
    entry = _ip_sets.get(key)
    if entry is not None:
        _, members = entry
        members.discard(job_id)


async def set_status(
    job_id: str, status: str, ttl_s: int | None = None,
    progress: dict | None = None,
) -> dict | None:
    doc = await get_job(job_id)
    if doc is None:
        return None
    # Terminal states win: never resurrect a cancelled/failed/done job.
    if doc.get("status") in TERMINAL and status in ("queued", "running"):
        return doc
    doc["status"] = status
    if progress is not None:
        doc["progress"] = progress
    await _write_doc(job_id, doc, ttl_s if ttl_s is not None else _ttl_for(status))
    if status in TERMINAL:
        await _remove_from_ip_sets(job_id, doc)
    return doc


async def set_progress(job_id: str, stage: str, done: int = 0, total: int = 0) -> None:
    doc = await get_job(job_id)
    if doc is None or doc.get("status") in TERMINAL:
        return
    doc["progress"] = {"stage": stage, "done": done, "total": total}
    await _write_doc(job_id, doc, _ttl_for(doc.get("status", "running")))


async def set_result(job_id: str, result: dict) -> None:
    doc = await get_job(job_id)
    if doc is None:
        return
    if doc.get("status") in TERMINAL:
        return
    doc["status"] = "done"
    doc["result"] = result
    doc["error"] = None
    await _write_doc(job_id, doc, RESULT_TTL_S)
    await _remove_from_ip_sets(job_id, doc)


async def set_error(job_id: str, code: str, message: str) -> None:
    doc = await get_job(job_id)
    if doc is None:
        return
    if doc.get("status") in TERMINAL:
        return
    doc["status"] = "failed"
    # Single level: GET /v1/jobs/{id} returns doc["error"] verbatim as the
    # response's `error` field, so pollers read body["error"]["code"] just
    # like the sync {"error": {"code", "message"}} envelope.
    doc["error"] = {"code": code, "message": message}
    doc["result"] = None
    await _write_doc(job_id, doc, FAILURE_TTL_S)
    await _remove_from_ip_sets(job_id, doc)


async def set_cancelled(job_id: str) -> None:
    doc = await get_job(job_id)
    if doc is None:
        return
    if doc.get("status") == "done":
        return
    doc["status"] = "cancelled"
    doc["result"] = None
    await _write_doc(job_id, doc, CANCELLED_TTL_S)
    await _remove_from_ip_sets(job_id, doc)


def _runner_key(worker_id: str) -> str:
    return f"runner:{worker_id}"


def _runner_alive(worker_id: str) -> bool:
    """Sync liveness check (no I/O): Redis key names are not readable without
    an await, so the runner heartbeat also stamps `_runner_live` in-process."""
    deadline = _runner_live.get(worker_id, 0.0)
    return deadline >= time.time()


def _runner_missing_heartbeats(worker_id: str) -> bool:
    return not _runner_alive(worker_id)


async def touch_runner(worker_id: str) -> None:
    """The runner heartbeat (issue #1061): refresh the worker's liveness TTL
    — one call per running task tick, not per doc. On Redis it is a plain
    SETEX; the in-memory deadline serves the degraded mode + tests."""
    now = time.time()
    _runner_live[worker_id] = now + RUNNER_TTL_S
    if _redis is not None:
        try:
            await _redis.setex(_runner_key(worker_id), RUNNER_TTL_S, now)
        except Exception as exc:
            log.warning("jobs.redis runner touch failed worker=%s: %s", worker_id[:8], exc)


async def claim_job(job_id: str, worker_id: str) -> None:
    """Stamp the owning runner onto the job doc (issue #1061) and touch its
    liveness. The stamp + the heartbeats are what the orphan sweep reads."""
    doc = await get_job(job_id)
    if doc is None or doc.get("status") in TERMINAL:
        return
    doc["runner"] = worker_id
    await _write_doc(job_id, doc, _ttl_for(doc.get("status", "running")))
    await touch_runner(worker_id)


async def reap_orphans(grace_s: float = CLAIM_GRACE_S) -> list[str]:
    """Mark every non-terminal job doc without a live runner as
    ``failed {orphaned}`` (issue #1061). The owning container died — no task
    will ever reach a terminal state for it. Called on worker startup and
    opportunistically by ``GET /v1/jobs/{id}`` poll checks. Returns the ids
    it reaped. Guard rail: a doc younger than ``grace_s`` is skipped — the
    enqueue→claim window is single-digit milliseconds, so younger docs are
    still being claimed.

    Without Redis (memory mode) there is nothing to survive a restart, so
    the same rules run against the in-memory docs (the tests use them)."""
    now = time.time()
    reaped: list[str] = []
    docs: list[tuple[str, dict]] = []
    if _redis is not None:
        try:
            cursor: int | str = 0
            while True:
                cursor, keys = await _redis.scan(
                    int(cursor), match="job:*", count=200
                )
                for key in keys:
                    name = key.decode() if isinstance(key, bytes) else key
                    if not name.startswith("job:"):
                        continue
                    job_id = name[4:]
                    raw = await _redis.get(name)
                    if raw is None:
                        continue
                    try:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        docs.append((job_id, json.loads(raw)))
                    except ValueError:
                        continue
                if int(cursor) == 0:
                    break
        except Exception as exc:
            log.warning("jobs.redis orphan scan failed: %s", exc)
            return reaped
    else:
        for key, entry in list(_memory.items()):
            if entry[0] <= now:
                continue
            docs.append((key.removeprefix("job:"), entry[1]))
    for job_id, doc in docs:
        status = doc.get("status", "queued")
        if status in TERMINAL:
            continue
        if now - float(doc.get("updated_at", 0.0)) < grace_s:
            continue
        runner = doc.get("runner")
        if runner and _runner_alive(runner):
            continue
        await set_error(
            job_id,
            "orphaned",
            "Job lost when the worker restarted before it finished — nothing owns it anymore.",
        )
        reaped.append(job_id)
        log.warning("jobs.orphan reaped id=%s type=%s", job_id[:12], doc.get("type"))
    return reaped


async def request_cancel(job_id: str) -> bool:
    """Mark cancelling (runners poll → cancelled). True when known."""
    doc = await get_job(job_id)
    if doc is None:
        return False
    if doc.get("status") in TERMINAL:
        return True
    doc["status"] = "cancelling"
    await _write_doc(job_id, doc, CANCELLED_TTL_S)
    if _redis is not None:
        try:
            await _redis.publish(f"job:cancel:{job_id}", "cancel")
        except Exception:
            pass
    return True


async def list_nonterminal_by_ip_type(ip: str, job_type: str) -> list[str]:
    out: list[str] = []
    if _redis is not None:
        try:
            members = await _redis.smembers(_ip_key(ip, job_type))
            ids = [
                m.decode() if isinstance(m, bytes) else str(m) for m in members
            ]
            for jid in ids:
                doc = await get_job(jid)
                if doc is not None and doc.get("status") not in TERMINAL:
                    out.append(jid)
            return out
        except Exception as exc:
            log.warning("jobs.redis smembers failed: %s", exc)
    _prune(time.time())
    entry = _ip_sets.get(_ip_key(ip, job_type))
    if entry is None:
        return []
    _, members = entry
    for jid in list(members):
        doc = _memory.get(_job_key(jid))
        if doc is not None and doc[0] > time.time() and doc[1].get("status") not in TERMINAL:
            out.append(jid)
    return out


async def count_nonterminal_by_ip(ip: str) -> int:
    if _redis is not None:
        try:
            total = 0
            async for key in _redis.scan_iter(f"ip:{ip}:*"):
                members = await _redis.smembers(key)
                for m in members:
                    jid = m.decode() if isinstance(m, bytes) else str(m)
                    doc = await get_job(jid)
                    if doc is not None and doc.get("status") not in TERMINAL:
                        total += 1
            return total
        except Exception:
            pass
    _prune(time.time())
    total = 0
    for key, (_, members) in _ip_sets.items():
        if key.startswith(f"ip:{ip}:"):
            for jid in members:
                doc = _memory.get(_job_key(jid))
                if doc is not None and doc[0] > time.time() and doc[1].get("status") not in TERMINAL:
                    total += 1
    # Fallback when ip sets were cleared (e.g. tests resetting redis):
    # scan memory docs directly so the per-IP cap still holds.
    if total == 0:
        for _, (_, doc) in _memory.items():
            if (
                doc.get("client_ip") == ip
                and doc.get("status") not in TERMINAL
            ):
                total += 1
    return total


async def expire_job(job_id: str) -> None:
    if _redis is not None:
        try:
            await _redis.delete(_job_key(job_id))
        except Exception:
            pass
    _memory.pop(_job_key(job_id), None)


def reset_memory() -> None:
    """Test helper: clear the fallback state."""
    _memory.clear()
    _ip_sets.clear()
    _pending.clear()
    _runner_live.clear()


__all__ = [
    "TERMINAL",
    "RUNNER_TTL_S",
    "CLAIM_GRACE_S",
    "configure_jobs_redis",
    "create_job",
    "get_job",
    "set_status",
    "set_progress",
    "set_result",
    "set_error",
    "set_cancelled",
    "request_cancel",
    "list_nonterminal_by_ip_type",
    "count_nonterminal_by_ip",
    "expire_job",
    "reset_memory",
]
