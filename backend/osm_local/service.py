"""In-process access to the local extract (no HTTP hop, no second app).

``fetch_local`` is what :func:`backend.citymap.overpass.fetch_overpass`
tries before the public mirrors. It returns Overpass ``elements``, or
``None`` whenever the extract cannot answer faithfully (feature off, file
missing, other query shape, unsupported filter, bbox not fully inside the
extract polygon, stale database) so the caller falls through unchanged.
The database file is re-opened when it is replaced (a rebuild lands with
an atomic rename), so no restart is needed after a refresh.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path

from backend.osm_local import ql
from backend.osm_local.store import Store

log = logging.getLogger("backend.osm_local")

_lock = threading.Lock()
_store: Store | None = None
_stamp: tuple[str, int, int] | None = None  # (path, mtime_ns, size)


_sem: asyncio.Semaphore | None = None
_sem_cap = 0


def _semaphore() -> asyncio.Semaphore:
    """Per-worker cap on concurrent queries (see ``local_osm_max_concurrent``)."""
    global _sem, _sem_cap
    from backend.citymap.config import settings

    want = max(1, int(settings.local_osm_max_concurrent))
    if _sem is None or _sem_cap != want:
        _sem, _sem_cap = asyncio.Semaphore(want), want
    return _sem


def _db_path() -> str:
    from backend.citymap.config import settings

    return settings.local_osm_db.strip()


def get_store() -> Store | None:
    """The open store, (re)opened when the configured file changed; None when off/unusable."""
    global _store, _stamp
    path = _db_path()
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    stamp = (path, st.st_mtime_ns, st.st_size)
    with _lock:
        if _store is not None and _stamp == stamp:
            return _store
        try:
            _store = Store(Path(path))
            _stamp = stamp
        except Exception as exc:  # corrupt/foreign file: behave as off
            log.warning("osm_local: cannot open %s: %r", path, exc)
            _store, _stamp = None, stamp
        return _store


def answer(query: str, min_len_m: float = 0.0) -> dict | None:
    store = get_store()
    if store is None or not store.fresh:
        return None
    try:
        members = ql.parse(query)
    except ql.QueryError:
        return None
    if not all(store.supports(m) and store.covers(m.bbox) for m in members):
        return None
    return store.run(members, min_len_m)


async def fetch_local(query: str, min_len_m: float = 0.0) -> list[dict] | None:
    """Elements for ``query`` from the local extract, or ``None`` to fall through."""
    if not _db_path():
        return None
    try:
        async with _semaphore():
            result = await asyncio.to_thread(answer, query, min_len_m)
        return None if result is None else result["elements"]
    except Exception:  # never let the local path break a render
        log.exception("osm_local: query failed, falling back to mirrors")
        return None


def reset() -> None:
    """Forget the open store (tests)."""
    global _store, _stamp, _sem, _sem_cap
    with _lock:
        _store, _stamp = None, None
    _sem, _sem_cap = None, 0
