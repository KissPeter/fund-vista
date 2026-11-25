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
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

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
        stl_result_ttl_hours: int = 24,
    ) -> None:
        self.images_dir = images_dir
        self.results_dir = results_dir
        self.ttl = timedelta(hours=ttl_hours)
        # Purchased designs (POST /v1/images/{id}/retain) live this long.
        # Anonymous previews keep ``ttl``. Marker files (``{id}.{ext}.retained``)
        # next to the image record the promotion; they survive restarts and
        # need no Redis.
        self.retained_ttl = timedelta(hours=retained_ttl_hours)
        # Derived STL artworks are regenerable: unordered views/sheets expire
        # after 24 h; the retained marker above extends them to 90 d.
        self.stl_result_ttl = timedelta(hours=stl_result_ttl_hours)
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

    def _image_is_retained(self, image_id: str) -> bool:
        marker_prefix = image_id + "."
        try:
            names = os.listdir(self.images_dir)
        except OSError:
            return False
        return any(
            name.startswith(marker_prefix) and name.endswith(".retained")
            for name in names
        )

    def _mark_retained(self, path: str, stamp: str) -> bool:
        """Write one TTL promotion marker; return True when newly created."""
        marker = self._retained_marker(path)
        if os.path.exists(marker):
            return False
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(stamp)
        return True

    def _ttl_for(self, path: str) -> timedelta:
        if os.path.exists(self._retained_marker(path)):
            return self.retained_ttl
        if os.path.basename(path).endswith((
            "_stlview.svg", "_stlsheet.svg",
            "_stlview.svg.json", "_stlsheet.svg.json",
        )):
            return self.stl_result_ttl
        return self.ttl

    def is_retained(self, path: str) -> bool:
        return os.path.exists(self._retained_marker(path))

    def _is_derived_stl_result(self, name: str, image_id: str) -> bool:
        """STL view/sheet files (and their sidecars) owned by ``image_id``."""
        if not name.endswith((
            "_stlview.svg", "_stlsheet.svg",
            "_stlview.svg.json", "_stlsheet.svg.json",
        )):
            return False
        # Both view and combined-sheet results are content-addressed, so the
        # source design is recorded in the sidecar the backend writes. The
        # sidecar image_id is the only ownership signal (never a filename
        # prefix), because result ids are SHA-256 of image + params + view.
        meta_file = name if name.endswith(".json") else name + ".json"
        path = os.path.join(self.results_dir, meta_file)
        try:
            with open(path, encoding="utf-8") as fh:
                return json.loads(fh.read()).get("image_id") == image_id
        except (OSError, ValueError):
            return False

    def retain_image(self, image_id: str) -> tuple[str, datetime] | None:
        """Promote an image and its derived STL results to the retained TTL.

        Returns ``(path, expires_at)`` or ``None`` when the image is
        missing/expired. Idempotent — re-retaining is a no-op. Derived results
        share the design's fate: an ordered custom sheet (and the view SVGs/
        sidecars it was built from) must survive to fulfillment day 90, while
        anonymous previews keep the short TTL (`docs/nfr-retention.md`).
        """
        path = self.find_image(image_id)
        if path is None:
            return None
        with self._lock:
            stamp = datetime.now(tz=timezone.utc).isoformat()
            if self._mark_retained(path, stamp):
                log.info("image %s retained", image_id[:12])
            # Marker names are the exact paths the lazy TTL checks: the SVG
            # itself and its `.json` sidecar both need one.
            try:
                names = os.listdir(self.results_dir)
            except OSError:
                names = []
            for name in names:
                if self._is_derived_stl_result(name, image_id):
                    self._mark_retained(os.path.join(self.results_dir, name), stamp)
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

    # -- result sidecar (P1: metadata for cache-first converts) ----------

    def result_meta_path(self, filename: str) -> str:
        """Sidecar JSON next to the SVG: stats/warnings/vpype_command."""
        return self.result_path(filename) + ".json"

    def put_result_meta(self, filename: str, meta: dict) -> str:
        """Persist the sidecar; the SVG itself must already be stored."""
        path = self.result_meta_path(filename)
        payload = json.dumps(meta, sort_keys=True, separators=(",", ":"))
        source_id = meta.get("image_id")
        source_retained = (
            filename.endswith(("_stlview.svg", "_stlsheet.svg"))
            and isinstance(source_id, str)
            and self._image_is_retained(source_id)
        )
        stamp = datetime.now(tz=UTC).isoformat()
        with self._lock:
            os.makedirs(self.results_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)
            # A view/sheet generated *after* retain inherits the ordered 90 d
            # TTL; otherwise newly selected views would vanish at 24 h.
            if source_retained:
                self._mark_retained(path, stamp)
                self._mark_retained(path[:-5], stamp)  # the SVG path
        return path

    def get_result_meta(self, filename: str) -> dict | None:
        """Return the stored sidecar dict, or None when missing/expired."""
        path = self.result_meta_path(filename)
        with self._lock:
            if not os.path.exists(path):
                return None
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = fh.read()
            except OSError:
                return None
        try:
            meta = json.loads(data)
            return meta if isinstance(meta, dict) else None
        except ValueError:
            return None
