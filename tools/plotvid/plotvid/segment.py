"""Segment candidates at pen events, and a weighted-interval DP to choose them.

A segment is a run of whole strokes, from event ``a`` to event ``k+1``:
- **End:** just after a long stroke that was drawn, so the clip leaves on
  a finished line rather than mid-air. The cut lands ``post_roll`` after
  the pen event that closes the stroke.
- **Start:** a few short strokes (``lead_short``, by default 2 or 3) before
  the next long stroke, so a burst of detail work leads into the long move.
  The cut lands ``pre_roll`` before the first event.

Both rolls are clamped to the midpoint with the neighbouring event, so a
cut never includes half of another servo sound.

When strict rules cannot fill a clip, relaxation levels apply:

| Level | Start rule | End rule |
|---|---|---|
| 0 | `lead_short` short strokes, then a long stroke | after a long drawn stroke |
| 1 | 0..max(`lead_short`)+1 strokes of any length before a long stroke | after a long drawn stroke |
| 2 | any event | after any stroke that may be drawn |
| 3 | any event | any event |

A candidate's level is the higher of its start and end levels. Its weight
is ``ink gained * relax_penalty ** level``. Ink gained is the summed new
ink of its strokes; when ink is unknown, the duration of strokes that may
be drawn stands in for it.

``choose`` runs the classic weighted-interval DP (sort by end, binary search
for the last compatible candidate). The result maximises total weight and
never overlaps.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from plotvid.config import SegmentConfig
from plotvid.events import Stroke

_EPS = 1e-6


@dataclass(frozen=True)
class Segment:
    source: int  # index into the ordered sources
    start_s: float  # cut-in, source time
    end_s: float  # cut-out, source time
    first_stroke: int
    last_stroke: int
    ink: float  # new ink over the strokes (px at the ink reference width)
    level: int  # relaxation level the rules needed
    weight: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    def overlaps(self, other: Segment) -> bool:
        return (self.source == other.source and self.start_s < other.end_s - _EPS
                and other.start_s < self.end_s - _EPS)


def _start_levels(strokes: list[Stroke], cfg: SegmentConfig) -> list[int]:
    """Lowest level at which event a (start of stroke a) may open a segment."""
    n = len(strokes)
    reach = max(cfg.lead_short) + 1
    out = []
    for a in range(n):
        level = 2
        for m in range(a, min(n, a + reach + 1)):
            if not strokes[m].long:
                continue
            lead = m - a
            if lead in cfg.lead_short and all(not strokes[i].long for i in range(a, m)):
                level = 0
            else:
                level = min(level, 1)
            break  # only the first long stroke ahead counts
        out.append(level)
    return out


def _end_level(s: Stroke) -> int:
    if s.long and s.maybe_drawn:
        return 0
    return 2 if s.maybe_drawn else 3


def candidates(strokes: list[Stroke], cfg: SegmentConfig, source: int = 0,
               max_level: int = 3, duration_s: float | None = None,
               window: tuple[float, float] | None = None) -> list[Segment]:
    """All segments obeying the rules up to ``max_level``, 3-8 s, inside ``window``."""
    n = len(strokes)
    if n == 0:
        return []
    events = [s.start_s for s in strokes] + [strokes[-1].end_s]
    starts = _start_levels(strokes, cfg)
    ends = [_end_level(s) for s in strokes]
    gain = []
    for s in strokes:
        if s.new_ink is not None and s.drawing is not None:
            gain.append(max(0.0, s.new_ink))
        else:
            gain.append(s.duration_s if s.maybe_drawn else 0.0)
    prefix = [0.0]
    for g in gain:
        prefix.append(prefix[-1] + g)

    def cut_in(a: int) -> float:
        lo = (events[a - 1] + events[a]) / 2 if a > 0 else 0.0
        return max(lo, events[a] - cfg.pre_roll)

    def cut_out(e: int) -> float:
        hi = (events[e] + events[e + 1]) / 2 if e + 1 < len(events) else (
            duration_s if duration_s is not None else events[e] + cfg.post_roll)
        return min(hi, events[e] + cfg.post_roll)

    lo_w, hi_w = window if window is not None else (float("-inf"), float("inf"))
    out: list[Segment] = []
    for a in range(n):
        if starts[a] > max_level:
            continue
        t0 = cut_in(a)
        if t0 < lo_w - _EPS:
            continue
        for k in range(a, n):
            t1 = cut_out(k + 1)
            if t1 - t0 > cfg.max_s + _EPS or t1 > hi_w + _EPS:
                break
            if t1 - t0 < cfg.min_s - _EPS:
                continue
            level = max(starts[a], ends[k])
            if level > max_level:
                continue
            ink = prefix[k + 1] - prefix[a]
            out.append(Segment(source, t0, t1, a, k, ink, level,
                               (ink + _EPS) * cfg.relax_penalty ** level))
    return out


def choose(cands: list[Segment]) -> list[Segment]:
    """Maximum-weight set of non-overlapping candidates (one source at a time)."""
    by_source: dict[int, list[Segment]] = {}
    for c in cands:
        by_source.setdefault(c.source, []).append(c)
    chosen: list[Segment] = []
    for src in sorted(by_source):
        segs = sorted(by_source[src], key=lambda c: (c.end_s, c.start_s))
        ends = [c.end_s for c in segs]
        best = [0.0] * (len(segs) + 1)
        take = [False] * len(segs)
        prev = [0] * len(segs)
        for j, c in enumerate(segs):
            # Number of candidates ending at or before c starts.
            prev[j] = bisect.bisect_right(ends, c.start_s + _EPS, 0, j)
            with_c = best[prev[j]] + c.weight
            take[j] = with_c > best[j]
            best[j + 1] = with_c if take[j] else best[j]
        j = len(segs)
        picked = []
        while j > 0:
            if take[j - 1]:
                picked.append(segs[j - 1])
                j = prev[j - 1]
            else:
                j -= 1
        chosen.extend(reversed(picked))
    return chosen
