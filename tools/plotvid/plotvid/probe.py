"""ffprobe wrapper: duration, display size after rotation, fps, creation time."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path


@dataclass(frozen=True)
class SourceInfo:
    path: Path
    duration_s: float
    # Stored (coded) size and the size a player shows after applying the
    # rotation side data; ffmpeg autorotates, so filters see the display size.
    width: int
    height: int
    rotation: int
    fps: float
    has_audio: bool
    created: datetime
    created_from: str  # "container" | "mtime"

    @property
    def display_size(self) -> tuple[int, int]:
        if self.rotation % 180:
            return self.height, self.width
        return self.width, self.height


def _ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)


def _rotation(stream: dict) -> int:
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            return int(round(float(sd["rotation"])))
    # Older containers carry it as a tag.
    tag = (stream.get("tags") or {}).get("rotate")
    return int(tag) if tag else 0


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def probe(path: Path) -> SourceInfo:
    data = _ffprobe(path)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError(f"{path}: no video stream")
    fmt = data.get("format", {})

    created = _parse_time((fmt.get("tags") or {}).get("creation_time")) or _parse_time(
        (video.get("tags") or {}).get("creation_time")
    )
    created_from = "container"
    if created is None:
        created = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        created_from = "mtime"

    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    fps = float(Fraction(rate)) if rate != "0/0" else 0.0

    return SourceInfo(
        path=path,
        duration_s=float(fmt.get("duration") or video.get("duration") or 0.0),
        width=int(video["width"]),
        height=int(video["height"]),
        rotation=_rotation(video),
        fps=fps,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        created=created,
        created_from=created_from,
    )


def order_sources(paths: tuple[Path, ...] | list[Path]) -> list[SourceInfo]:
    """Probe and sort by creation time; path name breaks ties deterministically."""
    infos = [probe(p) for p in paths]
    return sorted(infos, key=lambda i: (i.created, i.path.name))
