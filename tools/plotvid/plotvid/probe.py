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
    # Extra clockwise degrees applied on top of ffmpeg's autorotate (resolve_rotation).
    extra_rotation: int = 0

    @property
    def display_size(self) -> tuple[int, int]:
        if (self.rotation + self.extra_rotation) % 180:
            return self.height, self.width
        return self.width, self.height

    @property
    def rotate_filter(self) -> str:
        """ffmpeg filter for extra_rotation. It goes first in a video chain, since
        ffmpeg has already autorotated the frames by then."""
        return ROTATE_FILTERS[self.extra_rotation]


ROTATE_FILTERS = {0: "", 90: "transpose=1", 180: "hflip,vflip", 270: "transpose=2"}


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


def _tagged(stream: dict) -> bool:
    """True when the stream carries a rotation tag or Display Matrix side data."""
    if any("rotation" in sd for sd in stream.get("side_data_list", []) or []):
        return True
    return "rotate" in (stream.get("tags") or {})


def resolve_rotation(setting: str, tagged: bool, width: int, height: int) -> int:
    """Extra clockwise degrees on top of ffmpeg's autorotate.

    The Sony files are stored landscape with no rotation tag, although the camera
    was turned to portrait. So ``auto`` turns untagged landscape files 90 degrees
    counter-clockwise (270 clockwise). Tagged and portrait files need nothing."""
    if setting != "auto":
        return int(setting)
    return 270 if not tagged and width > height else 0


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def probe(path: Path, rotate: str = "auto") -> SourceInfo:
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
        extra_rotation=resolve_rotation(rotate, _tagged(video), int(video["width"]),
                                        int(video["height"])),
    )


def order_sources(paths: tuple[Path, ...] | list[Path],
                  rotate: str = "auto") -> list[SourceInfo]:
    """Probe and sort by creation time; path name breaks ties deterministically."""
    infos = [probe(p, rotate) for p in paths]
    return sorted(infos, key=lambda i: (i.created, i.path.name))
