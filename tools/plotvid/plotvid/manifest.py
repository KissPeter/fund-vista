"""Per-clip manifest JSON: what a clip is made of and why.

``plan`` writes the manifest and ``render`` reads it. A hand-edited or
hand-built manifest renders the same way, which is what the end-to-end
test does. Times are source seconds.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from plotvid import assign
from plotvid.assign import ClipPlan
from plotvid.events import Stroke
from plotvid.segment import Segment

VERSION = 2


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
        "part": seg.part,
        "final": seg.final,
        "strokes": [_stroke(s) for s in strokes[seg.first_stroke:seg.last_stroke + 1]],
    }


def clip_manifest(plot: str, clip: ClipPlan, sources: list[Path],
                  strokes: list[list[Stroke]], final_photo: dict[str, Any],
                  parts: list[tuple[float, float]],
                  extra: dict[str, Any] | None = None) -> dict[str, Any]:
    n = len(parts)
    segs = clip.segments
    return {
        "version": VERSION,
        "plot": plot,
        "clip": clip.clip + 1,
        # Story invariant: first segment from the first part, last from the last.
        "story": {"parts": n, "first_segment_part": segs[0].part,
                  "last_segment_part": segs[-1].part,
                  "ok": assign.story_ok(segs, n)},
        "parts": [{"index": i, "start_s": round(a, 3), "end_s": round(b, 3),
                   "timeline": "plotting time, sources end to end",
                   "segments": sum(s.part == i for s in segs),
                   "footage_s": round(sum(s.duration_s for s in segs if s.part == i), 3)}
                  for i, (a, b) in enumerate(parts)],
        "seed": clip.seed,
        "relax_level": clip.level,
        "max_segment_level": max(s.level for s in segs),
        "footage_s": round(clip.footage_s, 3),
        "length_s": round(clip.length_s, 3),
        "segments": [_segment(s, sources[s.source], strokes[s.source]) for s in segs],
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
