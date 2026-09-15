"""Filesystem image/result store with TTL.

- ``images/{sha256}.{ext}`` — original uploaded bytes (spec §5).
- ``results/{sha256}_{paramhash}_optimized.svg`` — convert outputs, content
  addressed so repeating the same convert is a cache hit (no recompute).
- TTL is enforced lazily on read (mtime + TTL); expired files are removed.
  Results share the same lazy TTL (review C.2.3) — they are deterministically
  regenerable, so expiring them on access bounds disk growth without costing
  correctness. This keeps v1 dependency-free (no S3/Redis needed) while the
  class boundary lets a future S3 store slot in without touching the router
  or pipeline.
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
    def __init__(
        self,
        *,
        images_dir: str,
        results_dir: str,
        ttl_hours: int = 48,
        retained_ttl_hours: int = 90 * 24,
    ) -> None:
        self.images_dir = images_dir
        self.results_dir = results_dir
        self.ttl = timedelta(hours=ttl_hours)
        # Purchased designs (POST /v1/images/{id}/retain) live this long.
        # Anonymous previews keep ``ttl``. Marker files (``{id}.{ext}.retained``)
        # next to the image record the promotion; they survive restarts and
        # need no Redis.
        self.retained_ttl = timedelta(hours=retained_ttl_hours)
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
        return datetime.now(tz=timezone.utc) - mtime > self._ttl_for(path)

    @staticmethod
    def _retained_marker(path: str) -> str:
        return path + ".retained"

    def _ttl_for(self, path: str) -> timedelta:
        if os.path.exists(self._retained_marker(path)):
            return self.retained_ttl
        return self.ttl

    def is_retained(self, path: str) -> bool:
        return os.path.exists(self._retained_marker(path))

    def retain_image(self, image_id: str) -> tuple[str, datetime] | None:
        """Promote an image to the retained (purchased-design) TTL.

        Returns ``(path, expires_at)`` or ``None`` when the image is
        missing/expired. Idempotent — re-retaining is a no-op.
        """
        path = self.find_image(image_id)
        if path is None:
            return None
        with self._lock:
            marker = self._retained_marker(path)
            if not os.path.exists(marker):
                with open(marker, "w", encoding="utf-8") as fh:
                    fh.write(datetime.now(tz=timezone.utc).isoformat())
                log.info("image %s retained", image_id[:12])
        return path, self.expires_at_for(path)

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
                if name.endswith(".retained") or name.endswith(".tmp"):
                    continue
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
        return mtime + self._ttl_for(path)

    # -- results --------------------------------------------------------

    def result_path(self, filename: str) -> str:
        # Guard against path traversal: only allow the basename we generated.
        safe = os.path.basename(filename)
        path = os.path.join(self.results_dir, safe)
        # Lazy TTL, same as images (review C.2.3): results are deterministically
        # regenerable, so an expired result is simply dropped — the caller sees
        # a missing file (404 result_not_found -> client re-runs the convert).
        if self._is_expired(path):
            try:
                os.remove(path)
            except OSError:
                pass
            log.debug("result %s expired", os.path.basename(path))
        return path

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
