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
* ``job:live:{id}`` — liveness key (issue #1061): the owning worker
  refreshes it while its task runs; when it expires the worker is gone
  (restart) and the job doc is an orphan. See :func:`reap_orphaned`.

Without Redis (tests, degraded mode) the same API runs on a process-local
dict with per-key expiry so contract tests stay hermetic. All Redis errors
degrade to a warning + miss/False rather than failing the request.
"""

from __future__ import annotations

import json
import logging
import time

from redis.exceptions import WatchError

from backend.jobs.schemas import (
    CANCELLED_TTL_S,
    FAILURE_TTL_S,
    LIVE_TTL_S,
    ORPHAN_GRACE_S,
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
_live: dict[str, float] = {}


def configure_jobs_redis(client) -> None:
    """Inject the shared Redis client (None = memory fallback)."""
    global _redis
    _redis = client
    _memory.clear()
    _ip_sets.clear()
    _pending.clear()
    _live.clear()


def _job_key(job_id: str) -> str:
    return f"job:{job_id}"


def _live_key(job_id: str) -> str:
    return f"job:live:{job_id}"


def _ip_key(ip: str, job_type: str) -> str:
    return f"ip:{ip}:{job_type}"


def _prune(now: float) -> None:
    for key in [k for k, (exp, _) in _memory.items() if exp <= now]:
        del _memory[key]
    for key in [k for k, (exp, _) in _ip_sets.items() if exp <= now]:
        del _ip_sets[key]
    for key in [k for k, exp in _live.items() if exp <= now]:
        del _live[key]


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


async def heartbeat(job_id: str) -> None:
    """Refresh the job's liveness key — its owner is alive.

    The runner task, the cancel poller and the runtime watchdog live in the
    memory of ONE worker: a worker or container restart loses them and
    nothing moves the job doc on any more (#1061). This key is the liveness
    signal the reaper reads; it expires ``LIVE_TTL_S`` after the last
    refresh.
    """
    if _redis is not None:
        try:
            await _redis.setex(_live_key(job_id), int(LIVE_TTL_S), "1")
            return
        except Exception as exc:
            log.warning("jobs.redis heartbeat failed id=%s: %s", job_id[:12], exc)
    now = time.time()
    _prune(now)
    _live[_live_key(job_id)] = now + LIVE_TTL_S


async def clear_heartbeat(job_id: str) -> None:
    """Drop the liveness key (the job reached a terminal state)."""
    if _redis is not None:
        try:
            await _redis.delete(_live_key(job_id))
        except Exception as exc:
            log.warning("jobs.redis heartbeat clear failed id=%s: %s", job_id[:12], exc)
    _live.pop(_live_key(job_id), None)


async def is_alive(job_id: str) -> bool:
    """True while a live owner refreshes the liveness key.

    Fail-safe: a Redis read error answers True (assume the owner lives), so
    a hiccup can never fail a healthy job. The process-local fallback is
    consulted too: the heartbeat writes it when its Redis write fails, so
    one broken write does not make a running job look dead.
    """
    if _redis is not None:
        try:
            if await _redis.exists(_live_key(job_id)):
                return True
        except Exception as exc:
            log.warning("jobs.redis liveness read failed id=%s: %s", job_id[:12], exc)
            return True
    _prune(time.time())
    exp = _live.get(_live_key(job_id))
    return exp is not None and exp > time.time()


async def _write_if_still_orphaned(job_id: str, doc: dict, ttl_s: int) -> bool:
    """Write the terminal doc only while the job is still an orphan.

    Redis: the liveness key is WATCHed, so a heartbeat that lands between
    the check and the write aborts the transaction — a live job is never
    failed by a racing reap. The process-local fallback has a single writer.
    """
    if await is_alive(job_id):
        return False
    if _redis is None:
        now = time.time()
        _prune(now)
        _memory[_job_key(job_id)] = (now + ttl_s, doc)
        return True
    try:
        async with _redis.pipeline(transaction=True) as pipe:
            await pipe.watch(_live_key(job_id))
            if await pipe.exists(_live_key(job_id)):
                await pipe.reset()
                return False
            pipe.multi()
            pipe.set(_job_key(job_id), json.dumps(doc), ex=ttl_s)
            await pipe.execute()
    except WatchError:
        # The owner refreshed the liveness key mid-write: it is alive.
        return False
    except Exception as exc:
        log.warning("jobs.orphan write failed id=%s: %s", job_id[:12], exc)
        return False
    now = time.time()
    _prune(now)
    _memory[_job_key(job_id)] = (now + ttl_s, doc)
    return True


async def reap_orphaned(job_id: str) -> dict | None:
    """Fail a job whose owner is gone (worker/container restart).

    The doc is the source of truth, but the thing that would move it on —
    the runner task — lives in one worker's memory. When that worker dies
    the doc stays ``running`` until its TTL expires and the client sees a
    404 instead of a terminal state (#1061). The liveness key tells the two
    apart: the owner refreshes it while the task runs, it expires when the
    owner is gone.

    A stale ``cancelling`` doc is the same leak with another terminal state:
    the client asked to cancel and nobody carried it out. It is written
    through the same guarded write, so an owner that is still alive (and
    about to finish) wins the race.

    Returns the job doc (reaped or untouched); None when the id is unknown.
    """
    doc = await get_job(job_id)
    if doc is None:
        return None
    status = doc.get("status")
    if status not in ("queued", "running", "cancelling"):
        return doc
    now = time.time()
    updated_at = float(doc.get("updated_at") or 0.0)
    if now - updated_at < ORPHAN_GRACE_S:
        return doc
    if await is_alive(job_id):
        return doc
    if status == "cancelling":
        terminal = {
            **doc,
            "status": "cancelled",
            "result": None,
            "updated_at": now,
        }
        if not await _write_if_still_orphaned(job_id, terminal, CANCELLED_TTL_S):
            return await get_job(job_id)
        await _remove_from_ip_sets(job_id, terminal)
        log.warning(
            "jobs.orphan id=%s type=%s was=cancelling", job_id[:12], doc.get("type")
        )
        return terminal
    failed = {
        **doc,
        "status": "failed",
        "error": {
            "code": "orphaned",
            "message": "The job's worker restarted before it finished.",
        },
        "result": None,
        "updated_at": now,
    }
    if not await _write_if_still_orphaned(job_id, failed, FAILURE_TTL_S):
        # The owner (or another worker) got there first — never overwrite.
        return await get_job(job_id)
    await _remove_from_ip_sets(job_id, failed)
    log.warning("jobs.orphan id=%s type=%s", job_id[:12], doc.get("type"))
    return failed


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
    _live.clear()


__all__ = [
    "TERMINAL",
    "configure_jobs_redis",
    "create_job",
    "get_job",
    "set_status",
    "set_progress",
    "set_result",
    "set_error",
    "set_cancelled",
    "request_cancel",
    "heartbeat",
    "clear_heartbeat",
    "is_alive",
    "reap_orphaned",
    "list_nonterminal_by_ip_type",
    "count_nonterminal_by_ip",
    "expire_job",
    "reset_memory",
]
