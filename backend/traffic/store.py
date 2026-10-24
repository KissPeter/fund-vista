"""Durable slice store for traffic day datasets (plan §6.5).

L0/L1 are the only data expensive to recreate (a 3.4 GB download each) and
must survive restarts, so they do NOT live in Redis. ``SliceStore`` is the
interface (``get / put / exists``); v1 ships ``FileSliceStore`` on the Mac
data volume (``PENPLOT_DATA_DIR/traffic`` → ``/data/traffic`` in prod).
``R2SliceStore`` (Phase 7) keeps the same keys — switching is a one-off
copy plus flipping ``TRAFFIC_STORE``.

Writes are atomic (temp file + rename); the ingest job writes L1 only after
the whole stream succeeded, so a cancelled/failed job never leaves a half
dataset. The temp file name is unique per write (pid + uuid), so two
concurrent ingests of the same day cannot interleave bytes and publish a
corrupt slice. Budget: ~2 MB per airport-day.

Slice encoding: gzip JSON (stdlib only). Evaluated 2026-10-11 and rejected:
MessagePack (~-20 % size for a new runtime dep — not worth it at ~2 MB
per slice) and Parquet/pyarrow (heavy native dep; columnar layout buys
nothing for small per-airport trace lists; slices stay human-debuggable
as JSON). Revisit if slices grow an order of magnitude (multi-day v2).
A representative slice shape lives in
``backend/tests/fixtures/traffic_day_lhbp.json`` (synthetic, 4 flights).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4


def _store_root() -> Path:
    from backend.penplot.router import _settings as _penplot_settings
    from backend.traffic.config import settings

    if settings.store_dir:
        return Path(settings.store_dir)
    return Path(_penplot_settings.data_dir) / "traffic"


def _resolve_inside(root: Path, key: str) -> Path:
    # Keys are fixed-shape store paths (pass/{date}.json, day/{date}/{icao}.json.gz).
    # Reject traversal so a bad ICAO can never escape the volume.
    parts = [p for p in key.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if not parts or parts != key.replace("\\", "/").split("/"):
        raise ValueError(f"Invalid store key '{key}'.")
    if any(p in ("", ".", "..") or "/" in p or "\\" in p for p in parts):
        raise ValueError(f"Invalid store key '{key}'.")
    target = root.joinpath(*parts)
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError(f"Invalid store key '{key}'.")
    return target


class SliceStore:
    """Minimal durable-store interface (plan §6.5)."""

    def exists(self, key: str) -> bool:  # noqa: D102
        raise NotImplementedError

    def get(self, key: str) -> bytes | None:  # noqa: D102
        raise NotImplementedError

    def put(self, key: str, data: bytes) -> None:  # noqa: D102
        raise NotImplementedError


class FileSliceStore(SliceStore):
    """Atomic-file store on the Mac data directory."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else _store_root()

    def _path(self, key: str) -> Path:
        return _resolve_inside(self.root, key)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def get(self, key: str) -> bytes | None:
        p = self._path(key)
        try:
            return p.read_bytes()
        except FileNotFoundError:
            return None

    def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp name: two concurrent ingests of the same day write the
        # same key, and a shared ``<name>.tmp`` would interleave their bytes
        # and publish a truncated gzip slice. Exclusive create ("xb") keeps a
        # name collision from clobbering another writer's temp file.
        tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            with open(tmp, "xb") as fh:
                fh.write(data)
            os.replace(tmp, p)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def default_store() -> SliceStore:
    """Store selected by ``TRAFFIC_STORE`` (v1 supports ``file`` only)."""
    from backend.traffic.config import settings

    if settings.store == "file":
        return FileSliceStore()
    raise ValueError(f"TRAFFIC_STORE '{settings.store}' not implemented yet (Phase 7).")


def check_free_space(min_free_bytes: int | None = None) -> None:
    """Raise ``OSError`` when the volume is below the free-space floor."""
    from backend.traffic.config import settings

    floor = settings.store_min_free_bytes if min_free_bytes is None else min_free_bytes
    root = _store_root()
    root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(str(root)).free
    if free < floor:
        raise OSError(f"Traffic store low on space ({free} bytes free, floor {floor}).")


def day_key(day_iso: str, icao: str) -> str:
    """L1 key for one airport/day (gzip JSON bytes)."""
    return f"day/{day_iso}/{icao.upper()}.json.gz"


def pass_key(day_iso: str) -> str:
    """L0 key: which airports a day's archive pass covered."""
    return f"pass/{day_iso}.json"


__all__ = [
    "SliceStore",
    "FileSliceStore",
    "default_store",
    "check_free_space",
    "day_key",
    "pass_key",
]
