"""Story parts and clip assignment: every clip tells the whole plot.

**Timeline.** Sources are laid end to end in recording order. A source's
stretch runs from its first pen event to its last, except the last source,
which runs on to the end of the recording so the pen's trip home can be
shown. That gives one *global* plotting time.

**Parts.** The global timeline is cut into ``videos.parts`` (3: beginning,
middle, end) equal time ranges. A part is only a range on the global
timeline, so it may later be backed by a different source file: nothing here
cares which source a segment comes from. A segment belongs to a part only if
it lies entirely inside it.

**Clips.** Every clip takes segments from every part, in chronological
order, so each clip shows the drawing grow from start to finish. Footage is
never reused: a segment goes to one clip or to none.

**Invariant.** In every clip the first segment comes from the first part and
the last segment from the last part (``story_ok``). The search only accepts
clips with every part present, so this holds by construction, and it is
checked explicitly before a plan is returned.

**Length.** A clip's length is footage + final photo + outro minus the
crossfade overlaps (each of the ``n + 1`` joins of ``n`` segments, photo and
outro shortens it by ``join_s``), and has to land in
``min_total_s``..``max_total_s`` (target ``target_s``). Cut rules, not
segment-length caps, make that possible, and the selection can choose
between segments of different lengths.

**Search**, for a clip count C and a relaxation level L (cut rules, see
``segment.py``; segments carry the level they needed):
1. Per part: candidates up to level L, then a weighted-interval DP gives a
   non-overlapping pool. The first draw maximises ink; later draws perturb
   the weights (seeded) so pools of different segment lengths are tried.
   The plot's final segment, which may run to the end of the source, is
   fixed first in the last part and goes to the last clip.
2. Segments are dealt out in blocks of C consecutive segments of a part, in
   a seeded random clip order. Each clip so gets picks spread over the whole
   part, and clips differ between seeds.
3. A local search (move a segment to another clip or to none, swap two)
   brings every clip inside the length window while keeping every part
   present and the parts balanced.

``count: auto`` tries C from ``max_count`` down and, per C, L from 0 up to
``max_relax``: the first C that reaches the minimum length in every clip at
some L wins. Clips are never shortened to fit more of them; if no count
works, no clips are planned and the report says so.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from plotvid.config import SegmentConfig, VideosConfig
from plotvid.events import Stroke
from plotvid.segment import Segment, candidates, choose

_BIG = 100.0  # cost per second outside the length window
_EPS = 1e-6


@dataclass(frozen=True)
class SourceTimeline:
    index: int
    strokes: list[Stroke]
    duration_s: float
    offset_s: float  # global time of this source's first event
    tail: bool = False  # the stretch runs on to the end of the recording

    @property
    def first_s(self) -> float:
        return self.strokes[0].start_s if self.strokes else 0.0

    @property
    def span_s(self) -> float:
        if not self.strokes:
            return 0.0
        return (self.duration_s if self.tail else self.strokes[-1].end_s) - self.first_s

    def to_global(self, t: float) -> float:
        return self.offset_s + (t - self.first_s)

    def to_local(self, g: float) -> float:
        return self.first_s + (g - self.offset_s)


def timelines(per_source: list[tuple[list[Stroke], float]]) -> list[SourceTimeline]:
    out, offset = [], 0.0
    for i, (strokes, duration) in enumerate(per_source):
        tl = SourceTimeline(i, strokes, duration, offset, tail=i == len(per_source) - 1)
        out.append(tl)
        offset += tl.span_s
    return out


@dataclass(frozen=True)
class Budget:
    """How a clip's length follows from its plotting footage."""
    ending_s: float  # final photo + outro
    join_s: float = 0.0  # overlap at each join (0 = hard cuts)

    def total(self, footage_s: float, n_segments: int) -> float:
        # n segments + photo + outro = n + 2 items, n + 1 joins.
        return footage_s + self.ending_s - self.join_s * (n_segments + 1)


@dataclass(frozen=True)
class ClipPlan:
    clip: int  # 0-based
    segments: list[Segment]  # chronological; each knows its part
    seed: int
    level: int  # relaxation level the plan was built at
    footage_s: float
    length_s: float  # whole clip: footage + photo + outro - overlaps


@dataclass(frozen=True)
class Assignment:
    clips: list[ClipPlan]
    count: int  # clips planned (0 = nothing feasible)
    level: int | None  # relaxation level used
    parts: list[tuple[float, float]]  # global plotting time per part
    pool_s: list[float]  # footage the part pools offered
    tried: dict[int, int | None]  # count -> level that worked (None = none did)

    @property
    def used_s(self) -> list[float]:
        used = [0.0] * len(self.parts)
        for c in self.clips:
            for s in c.segments:
                used[s.part] += s.duration_s
        return used


def parts_for(tls: list[SourceTimeline], count: int) -> list[tuple[float, float]]:
    total = sum(tl.span_s for tl in tls)
    edges = np.linspace(0.0, total, count + 1)
    return [(float(edges[i]), float(edges[i + 1])) for i in range(count)]


def part_candidates(tls: list[SourceTimeline], index: int, part: tuple[float, float],
                    seg: SegmentConfig, level: int) -> list[Segment]:
    out: list[Segment] = []
    for tl in tls:
        g0, g1 = tl.offset_s, tl.offset_s + tl.span_s
        lo, hi = max(part[0], g0), min(part[1], g1)
        if hi - lo < seg.min_s:
            continue
        # Edge parts keep the lead-in before the first event / the tail.
        lo_l = tl.to_local(lo) if lo > g0 + 1e-9 else float("-inf")
        hi_l = tl.to_local(hi) if hi < g1 - 1e-9 else float("inf")
        out += [replace(c, part=index) for c in candidates(
            tl.strokes, seg, tl.index, level, tl.duration_s, (lo_l, hi_l), tail=tl.tail)]
    return out


def _pool(cands: list[Segment], rng: np.random.Generator | None, jitter: float) -> list[Segment]:
    """Non-overlapping segments for one part. A final segment is fixed first."""
    finals = [c for c in cands if c.final]
    rest = [c for c in cands if not c.final]
    head: list[Segment] = []
    if finals:
        # Strictest rules first, then the most ink.
        fin = max(finals, key=lambda c: (-c.level, c.weight))
        head = [fin]
        rest = [c for c in rest if c.end_s <= fin.start_s + _EPS or c.source != fin.source]
    if rng is not None and jitter > 0:
        orig = {(c.source, c.start_s, c.end_s): c.weight for c in rest}
        noisy = [replace(c, weight=c.weight * float(np.exp(rng.normal(0.0, jitter))))
                 for c in rest]
        picked = [replace(c, weight=orig[(c.source, c.start_s, c.end_s)])
                  for c in choose(noisy)]
    else:
        picked = choose(rest)
    return sorted(picked + head, key=lambda c: (c.source, c.start_s))


def _max_cover_s(cands: list[Segment]) -> float:
    """Most footage any non-overlapping set of these candidates can hold."""
    return sum(c.duration_s for c in choose([replace(c, weight=c.duration_s) for c in cands]))


def _deal(segs: list[Segment], count: int, pins: dict[int, int],
          rng: np.random.Generator) -> list[int]:
    """Blocks of ``count`` consecutive segments of a part go to clips in a
    random order, so each clip's picks are spread over the whole part."""
    asg = [-1] * len(segs)
    by_part: dict[int, list[int]] = {}
    for i, s in enumerate(segs):
        by_part.setdefault(s.part, []).append(i)
    for idx in by_part.values():
        idx.sort(key=lambda i: (segs[i].source, segs[i].start_s))
        for b in range(0, len(idx), count):
            for i, c in zip(idx[b:b + count], rng.permutation(count)):
                asg[i] = int(c)
    for i, c in pins.items():
        asg[i] = c
    return asg


def story_ok(segments: list[Segment], n_parts: int) -> bool:
    """First segment from the first part, last from the last, parts in order,
    every part present."""
    parts = [s.part for s in segments]
    return (bool(parts) and parts[0] == 0 and parts[-1] == n_parts - 1
            and parts == sorted(parts) and set(parts) == set(range(n_parts)))


def _clip_cost(foot: list[float], m: int, budget: Budget, vids: VideosConfig) -> float:
    if m == 0:
        return _BIG * 100
    total = sum(foot)
    length = budget.total(total, m)
    cost = _BIG * (max(0.0, vids.min_total_s - length) + max(0.0, length - vids.max_total_s))
    cost += abs(length - vids.target_s)
    cost += _BIG * 10 * sum(1 for f in foot if f <= 0)  # a part missing
    cost += 0.3 * sum(abs(f - total / len(foot)) for f in foot)  # parts balanced
    return cost


def _in_window(foot: list[float], m: int, budget: Budget, vids: VideosConfig) -> bool:
    if m == 0 or any(f <= 0 for f in foot):
        return False
    return vids.min_total_s - _EPS <= budget.total(sum(foot), m) <= vids.max_total_s + _EPS


def _search(segs: list[Segment], count: int, n_parts: int, budget: Budget, vids: VideosConfig,
            rng: np.random.Generator) -> tuple[list[int], float, bool]:
    """Local search over segment -> clip (or -1 = unused). Returns the
    assignment, its cost and whether every clip is inside the length window."""
    n = len(segs)
    pins = {i: count - 1 for i, s in enumerate(segs) if s.final}
    wmax = max((s.weight for s in segs), default=1.0) or 1.0
    asg = _deal(segs, count, pins, rng)
    foot = [[0.0] * n_parts for _ in range(count)]
    cnt = [0] * count
    for i, c in enumerate(asg):
        if c >= 0:
            foot[c][segs[i].part] += segs[i].duration_s
            cnt[c] += 1
    cc = [_clip_cost(foot[c], cnt[c], budget, vids) for c in range(count)]

    def seg_term(i: int, c: int) -> float:
        if c < 0:
            return 20.0 if i in pins else 0.0  # the plot's ending should be shown
        return -0.2 * segs[i].weight / wmax  # prefer inky segments

    def delta_move(i: int, b: int) -> float:
        a, p, d = asg[i], segs[i].part, segs[i].duration_s
        out = seg_term(i, b) - seg_term(i, a)
        if a >= 0:
            foot[a][p] -= d
            cnt[a] -= 1
            out += _clip_cost(foot[a], cnt[a], budget, vids) - cc[a]
            foot[a][p] += d
            cnt[a] += 1
        if b >= 0:
            foot[b][p] += d
            cnt[b] += 1
            out += _clip_cost(foot[b], cnt[b], budget, vids) - cc[b]
            foot[b][p] -= d
            cnt[b] -= 1
        return out

    def move(i: int, b: int) -> None:
        a, p, d = asg[i], segs[i].part, segs[i].duration_s
        if a >= 0:
            foot[a][p] -= d
            cnt[a] -= 1
            cc[a] = _clip_cost(foot[a], cnt[a], budget, vids)
        if b >= 0:
            foot[b][p] += d
            cnt[b] += 1
            cc[b] = _clip_cost(foot[b], cnt[b], budget, vids)
        asg[i] = b

    def delta_swap(i: int, j: int) -> float:
        a, b = asg[i], asg[j]
        pi, pj, di, dj = segs[i].part, segs[j].part, segs[i].duration_s, segs[j].duration_s
        foot[a][pi] -= di
        foot[a][pj] += dj
        foot[b][pj] -= dj
        foot[b][pi] += di
        out = (_clip_cost(foot[a], cnt[a], budget, vids) - cc[a]
               + _clip_cost(foot[b], cnt[b], budget, vids) - cc[b])
        foot[a][pi] += di
        foot[a][pj] -= dj
        foot[b][pj] += dj
        foot[b][pi] -= di
        return out

    for _ in range(60):
        improved = False
        for i in rng.permutation(n):
            i = int(i)
            options = [-1, pins[i]] if i in pins else [-1, *range(count)]
            best_b, best_d = None, -1e-9
            for b in options:
                if b != asg[i] and (d := delta_move(i, b)) < best_d:
                    best_b, best_d = b, d
            if best_b is not None:
                move(i, best_b)
                improved = True
        for i in rng.permutation(n):
            i = int(i)
            for j in range(n):
                a, b = asg[i], asg[j]
                if a < 0 or b < 0 or a == b or i in pins or j in pins:
                    continue
                if delta_swap(i, j) < -1e-9:
                    pi, pj = segs[i].part, segs[j].part
                    di, dj = segs[i].duration_s, segs[j].duration_s
                    foot[a][pi] -= di
                    foot[a][pj] += dj
                    foot[b][pj] -= dj
                    foot[b][pi] += di
                    cc[a] = _clip_cost(foot[a], cnt[a], budget, vids)
                    cc[b] = _clip_cost(foot[b], cnt[b], budget, vids)
                    asg[i], asg[j] = b, a
                    improved = True
        if not improved:
            break
    total = sum(cc) + sum(seg_term(i, c) for i, c in enumerate(asg))
    ok = all(_in_window(foot[c], cnt[c], budget, vids) for c in range(count))
    return asg, total, ok


def _plan_for(cands_by_part: list[list[Segment]], count: int, level: int, budget: Budget,
              vids: VideosConfig) -> tuple[list[ClipPlan], list[float]] | None:
    """Best clips for ``count`` clips at one relaxation level, or None."""
    n_parts = len(cands_by_part)
    # Footage no pool can exceed: skip levels that cannot possibly be enough.
    need = count * (vids.min_total_s - budget.ending_s + budget.join_s)
    if sum(_max_cover_s(c) for c in cands_by_part) < need - _EPS:
        return None
    best = None
    for attempt in range(vids.attempts):
        rng = np.random.default_rng([vids.seed, count, level, attempt])
        pools = [_pool(c, rng if attempt else None, 0.35) for c in cands_by_part]
        segs = [s for pool in pools for s in pool]
        if sum(s.duration_s for s in segs) < need - _EPS or len(segs) < count * n_parts:
            continue
        asg, cost, ok = _search(segs, count, n_parts, budget, vids, rng)
        if ok and all(story_ok([s for s, a in zip(segs, asg) if a == c], n_parts)
                      for c in range(count)) and (best is None or cost < best[0]):
            best = (cost, segs, asg, [sum(s.duration_s for s in p) for p in pools])
    if best is None:
        return None
    _, segs, asg, pool_s = best
    clips = []
    for c in range(count):
        mine = sorted((s for s, a in zip(segs, asg) if a == c),
                      key=lambda s: (s.part, s.source, s.start_s))
        footage = sum(s.duration_s for s in mine)
        clips.append(ClipPlan(c, mine, vids.seed, level, footage, budget.total(footage, len(mine))))
    return clips, pool_s


def assign(tls: list[SourceTimeline], seg: SegmentConfig, vids: VideosConfig,
           budget: Budget) -> Assignment:
    parts = parts_for(tls, vids.parts)
    counts = [vids.count] if isinstance(vids.count, int) else list(range(vids.max_count, 0, -1))
    cands = {lv: [part_candidates(tls, i, p, seg, lv) for i, p in enumerate(parts)]
             for lv in range(seg.max_relax + 1)}
    tried: dict[int, int | None] = {}
    for count in counts:
        for level in range(seg.max_relax + 1):
            found = _plan_for(cands[level], count, level, budget, vids)
            if found:
                tried[count] = level
                return Assignment(found[0], count, level, parts, found[1], tried)
        tried[count] = None
    return Assignment([], 0, None, parts, [0.0] * len(parts), tried)
