"""Filesystem image/result store with TTL.

- ``images/{sha256}.{ext}`` — original uploaded bytes (spec §5).
- ``results/{sha256}_{paramhash}_optimized.svg`` — convert outputs, content
  addressed so repeating the same convert is a cache hit (no recompute).
- TTL is enforced lazily on read (mtime + TTL); expired files are removed.
  This keeps v1 dependency-free (no S3/Redis needed) while the class boundary
  lets a future S3 store slot in without touching the router or pipeline.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredImage:
    image_id: str
    path: str
    ext: str
    width: int
    height: int
    is_vector: bool
    expires_at: datetime


class ImageStore:
    def __init__(self, *, images_dir: str, results_dir: str, ttl_hours: int = 48) -> None:
        self.images_dir = images_dir
        self.results_dir = results_dir
        self.ttl = timedelta(hours=ttl_hours)
        self._lock = threading.Lock()
        os.makedirs(self.images_dir, exist_ok=True)
        os.makedirs(self.results_dir, exist_ok=True)

    # -- images ---------------------------------------------------------

    @staticmethod
    def sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _image_path(self, image_id: str, ext: str) -> str:
        return os.path.join(self.images_dir, f"{image_id}.{ext}")

    def _is_expired(self, path: str) -> bool:
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
        except OSError:
            return True
        return datetime.now(tz=timezone.utc) - mtime > self.ttl

    def put_image_bytes(self, data: bytes, ext: str) -> str:
        """Persist raw bytes if new; return the canonical image_id (sha256)."""
        image_id = self.sha256(data)
        path = self._image_path(image_id, ext)
        with self._lock:
            if not os.path.exists(path):
                tmp = path + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, path)
                log.debug("stored image %s (%d bytes)", image_id[:12], len(data))
            else:
                # Touch only if expired check would delete it — instead refresh
                # mtime on re-upload so TTL slides for actively used images.
                if self._is_expired(path):
                    with open(path, "wb") as fh:
                        fh.write(data)
        return image_id

    def find_image(self, image_id: str) -> str | None:
        """Return the on-disk path, or None if missing/expired."""
        with self._lock:
            for name in os.listdir(self.images_dir):
                if name.startswith(image_id + "."):
                    path = os.path.join(self.images_dir, name)
                    if self._is_expired(path):
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                        log.debug("image %s expired", image_id[:12])
                        return None
                    return path
        return None

    def expires_at_for(self, path: str) -> datetime:
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
        except OSError:
            mtime = datetime.now(tz=timezone.utc)
        return mtime + self.ttl

    # -- results --------------------------------------------------------

    def result_path(self, filename: str) -> str:
        # Guard against path traversal: only allow the basename we generated.
        safe = os.path.basename(filename)
        return os.path.join(self.results_dir, safe)

    def put_result(self, filename: str, svg_text: str) -> str:
        path = self.result_path(filename)
        with self._lock:
            if not os.path.exists(path):
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(svg_text)
                os.replace(tmp, path)
        return path

    def has_result(self, filename: str) -> bool:
        return os.path.exists(self.result_path(filename))
