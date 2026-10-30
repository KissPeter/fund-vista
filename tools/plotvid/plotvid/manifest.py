"""Per-clip manifest JSON: what a clip is made of and why.

``plan`` writes the manifest and ``render`` reads it. A hand-edited or
hand-built manifest renders the same way, which is what the end-to-end
test does. Times are source seconds.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from plotvid.assign import ClipPlan
from plotvid.events import Stroke
from plotvid.segment import Segment

VERSION = 1


def _stroke(s: Stroke) -> dict[str, Any]:
    return {"index": s.index, "start_s": round(s.start_s, 3), "end_s": round(s.end_s, 3),
            "duration_s": round(s.duration_s, 3),
            "new_ink": None if s.new_ink is None else round(s.new_ink, 1),
            "drawing": s.drawing, "long": s.long}


def _segment(seg: Segment, source: Path, strokes: list[Stroke]) -> dict[str, Any]:
    return {
        "source": str(source),
        "source_index": seg.source,
        "start_s": round(seg.start_s, 3),
        "end_s": round(seg.end_s, 3),
        "duration_s": round(seg.duration_s, 3),
        "ink": round(seg.ink, 1),
        "weight": round(seg.weight, 3),
        "relax_level": seg.level,
        "strokes": [_stroke(s) for s in strokes[seg.first_stroke:seg.last_stroke + 1]],
    }


def clip_manifest(plot: str, clip: ClipPlan, sources: list[Path],
                  strokes: list[list[Stroke]], final_photo: dict[str, Any],
                  extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "version": VERSION,
        "plot": plot,
        "clip": clip.clip + 1,
        "zone": {"index": clip.clip, "start_s": round(clip.zone[0], 3),
                 "end_s": round(clip.zone[1], 3), "timeline": "plotting time, sources end to end"},
        "seed": clip.seed,
        "relax_level": clip.level,
        "segment_cap_s": clip.cap_s,
        "pool_size": clip.pool,
        "footage_s": round(clip.total_s, 3),
        "segments": [_segment(s, sources[s.source], strokes[s.source]) for s in clip.segments],
        "final_photo": final_photo,
        **(extra or {}),
    }


def write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def read(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if data.get("version") != VERSION:
        raise ValueError(f"{path}: unsupported manifest version {data.get('version')}")
    for seg in data.get("segments", []):
        if not 0 <= seg["start_s"] < seg["end_s"]:
            raise ValueError(f"{path}: bad segment times {seg['start_s']}..{seg['end_s']}")
    return data
