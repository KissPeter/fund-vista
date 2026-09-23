"""Content-addressed disk cache for :func:`backend.penplot.imaging.parse_svg_vectors`.

Parsing a dense SVG (tens of thousands of path commands) costs seconds of
single-core CPU inside ``run_convert`` and is a pure function of the image
bytes. The result cache (P1) already makes repeated converts with the same
effective params cheap, but every slider tweak re-parses the SVG from scratch.
This cache keys on ``image_id`` (the sha256 of the SVG bytes), so the parse
runs exactly once per unique upload regardless of how the params change.

Same lazy-expiry + atomic-rename pattern as :class:`ImageStore`: entries
expire by mtime past ``ttl_hours`` and are removed on the read that finds
them expired. Never raises on I/O problems — the caller falls back to a
fresh parse.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

_lock = threading.Lock()


def parsed_path(image_id: str, parsed_dir: str) -> str:
    """Cache file for one image (image_id is already a hex digest)."""
    return os.path.join(parsed_dir, f"{image_id}.json")


def load_parsed_svg(
    image_id: str, parsed_dir: str, ttl_hours: int
) -> tuple[list[list[tuple[float, float]]], float, float, list[str]] | None:
    """Return cached ``(polylines, width, height, warnings)`` or None.

    Polylines come back as ``[(x, y), ...]`` lists of float tuples, i.e. the
    same structure ``parse_svg_vectors`` produces (the JSON round-trip
    restores float values exactly because Python's repr round-trips).
    """
    if not image_id or not ttl_hours:
        return None
    path = parsed_path(image_id, parsed_dir)
    with _lock:
        if not os.path.exists(path):
            return None
        try:
            if (time.time() - os.path.getmtime(path)) > ttl_hours * 3600.0:
                os.remove(path)
                return None
            with open(path, "r", encoding="utf-8") as fh:
                data = fh.read()
        except OSError:
            return None
    if not data:
        return None
    try:
        payload = json.loads(data)
        polylines = [
            [tuple(pt) for pt in pl] for pl in payload["polylines"]
        ]
        return (
            polylines,
            float(payload["width"]),
            float(payload["height"]),
            list(payload.get("warnings", [])),
        )
    except (TypeError, KeyError, ValueError):
        log.warning("parsed.svg cache corrupt for %s; dropping", image_id[:12])
        try:
            os.remove(path)
        except OSError:
            pass
        return None


def store_parsed_svg(
    image_id: str,
    parsed_dir: str,
    ttl_hours: int,
    polylines: list[list[tuple[float, float]]],
    width: float,
    height: float,
    warnings: list[str],
) -> None:
    """Persist a parse result; failures are logged and never raised."""
    if not image_id or not ttl_hours:
        return
    try:
        payload = json.dumps(
            {
                "polylines": polylines,
                "width": width,
                "height": height,
                "warnings": warnings,
            },
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return
    path = parsed_path(image_id, parsed_dir)
    tmp = f"{path}.tmp"
    with _lock:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            log.warning("parsed.svg cache write failed for %s", image_id[:12])