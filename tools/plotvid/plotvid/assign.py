"""Zones and clip assignment: each clip shows a different stage of the plot.

The plotting timeline runs from each source's first to last pen event,
with sources laid end to end in recording order. It is split into N
equal zones, and clip k only uses footage from zone k. A segment belongs
to a zone only if it lies entirely inside it, so no footage can be used
twice.

Per zone, relaxation levels are tried in order 0..max_relax:
1. Build candidates up to that level.
2. Run the DP to get a non-overlapping set. Segment-length caps
   (max_s, max_s-1, ... down to just above min_s) are tried before moving
   to the next level.
3. Stop at the first level whose set has a subset within
   ``min_total_s``..``max_total_s``.

From that set the clip takes a subset of segments with a total between
``min_total_s`` and ``max_total_s``, as close to ``target_s`` as possible.
Subsets are drawn with seeded weighted sampling: segments with more ink
come first more often. ``attempts`` draws are made, and the best fit wins,
with total weight breaking ties. Segments play in chronological order.

``count: auto`` picks the largest N up to ``max_count`` for which every
zone can be filled. The rules are never relaxed into clips shorter than
``min_total_s``: a zone that cannot reach it is reported as infeasible.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from plotvid.config import SegmentConfig, VideosConfig
from plotvid.events import Stroke
from plotvid.segment import Segment, candidates, choose


@dataclass(frozen=True)
class SourceTimeline:
    index: int
    strokes: list[Stroke]
    duration_s: float
    offset_s: float  # global time of this source's first event

    @property
    def first_s(self) -> float:
        return self.strokes[0].start_s if self.strokes else 0.0

    @property
    def span_s(self) -> float:
        return self.strokes[-1].end_s - self.first_s if self.strokes else 0.0

    def to_global(self, t: float) -> float:
        return self.offset_s + (t - self.first_s)

    def to_local(self, g: float) -> float:
        return self.first_s + (g - self.offset_s)


def timelines(per_source: list[tuple[list[Stroke], float]]) -> list[SourceTimeline]:
    out, offset = [], 0.0
    for i, (strokes, duration) in enumerate(per_source):
        tl = SourceTimeline(i, strokes, duration, offset)
        out.append(tl)
        offset += tl.span_s
    return out


@dataclass(frozen=True)
class ClipPlan:
    clip: int  # 0-based
    zone: tuple[float, float]  # global plotting time
    level: int  # relaxation level allowed in this zone
    pool: int  # segments the DP offered
    segments: list[Segment]
    seed: int
    cap_s: float  # longest segment allowed in this zone

    @property
    def total_s(self) -> float:
        return sum(s.duration_s for s in self.segments)


@dataclass(frozen=True)
class ZoneResult:
    zone: tuple[float, float]
    level: int | None  # None = infeasible at every level
    pool: list[Segment]
    cap_s: float | None  # segment length cap that made the zone fit


def _caps(seg: SegmentConfig) -> list[float]:
    caps, cap = [], seg.max_s
    while cap > seg.min_s + 1e-6:
        caps.append(cap)
        cap -= 1.0
    return caps + [seg.min_s + 0.5]


def zone_pool(tls: list[SourceTimeline], zone: tuple[float, float], seg: SegmentConfig,
              vids: VideosConfig) -> ZoneResult:
    pool: list[Segment] = []
    for level in range(seg.max_relax + 1):
        cands: list[Segment] = []
        for tl in tls:
            g0, g1 = tl.offset_s, tl.offset_s + tl.span_s
            lo, hi = max(zone[0], g0), min(zone[1], g1)
            if hi - lo < seg.min_s:
                continue
            # Edge zones keep the lead-in before the first event / the tail.
            lo_l = tl.to_local(lo) if lo > g0 else float("-inf")
            hi_l = tl.to_local(hi) if hi < g1 else float("inf")
            cands += candidates(tl.strokes, seg, tl.index, level, tl.duration_s, (lo_l, hi_l))
        # The DP maximises ink, which favours long segments. Their sums can
        # step over the min_total_s..max_total_s window (for example 2 x 6.9 s
        # is too short and 3 x 6.9 s too long). Shorter segment caps come
        # before looser cut rules.
        for cap in _caps(seg):
            pool = choose([c for c in cands if c.duration_s <= cap + 1e-6])
            if (sum(s.duration_s for s in pool) >= vids.min_total_s
                    and pick_subset(pool, vids, np.random.default_rng(0))):
                return ZoneResult(zone, level, pool, cap)
    return ZoneResult(zone, None, pool, None)


def pick_subset(pool: list[Segment], vids: VideosConfig, rng: np.random.Generator) -> list[Segment]:
    """Seeded weighted subset with total in [min_total_s, max_total_s], nearest target."""
    if not pool:
        return []
    w = np.array([max(s.weight, 1e-9) for s in pool])
    d = np.array([s.duration_s for s in pool])
    best_key, best = None, []
    for _ in range(vids.attempts):
        # Efraimidis-Spirakis: sorting by u**(1/w) samples without replacement
        # in proportion to weight.
        keys = rng.random(len(pool)) ** (1.0 / (w / w.max()))
        total, picked = 0.0, []
        for i in np.argsort(-keys):
            if total + d[i] <= vids.max_total_s + 1e-6:
                picked.append(int(i))
                total += d[i]
                if total >= vids.target_s:
                    break
        if total < vids.min_total_s - 1e-6:
            continue
        key = (round(abs(total - vids.target_s), 3), -float(w[picked].sum()))
        if best_key is None or key < best_key:
            best_key, best = key, picked
    segs = [pool[i] for i in best]
    return sorted(segs, key=lambda s: (s.source, s.start_s))


def zones_for(tls: list[SourceTimeline], count: int) -> list[tuple[float, float]]:
    total = sum(tl.span_s for tl in tls)
    edges = np.linspace(0.0, total, count + 1)
    return [(float(edges[i]), float(edges[i + 1])) for i in range(count)]


@dataclass(frozen=True)
class Assignment:
    clips: list[ClipPlan]
    count: int
    infeasible_zones: list[int]  # zones that could not reach min_total_s
    tried: dict[int, list[int]]  # count -> infeasible zone indices (auto mode)


def assign(tls: list[SourceTimeline], seg: SegmentConfig, vids: VideosConfig) -> Assignment:
    counts = [vids.count] if isinstance(vids.count, int) else list(range(vids.max_count, 0, -1))
    tried: dict[int, list[int]] = {}
    results: list[ZoneResult] = []
    count = counts[-1]
    for n in counts:
        results = [zone_pool(tls, z, seg, vids) for z in zones_for(tls, n)]
        bad = [i for i, r in enumerate(results) if r.level is None]
        tried[n] = bad
        count = n
        if not bad:
            break
    infeasible = tried[count]
    clips: list[ClipPlan] = []
    for k, r in enumerate(results):
        if r.level is None:
            continue
        # One RNG per clip, derived from the seed and the clip index, so a
        # clip's picks do not change when another zone changes.
        rng = np.random.default_rng([vids.seed, k])
        segs = pick_subset(r.pool, vids, rng)
        if segs:
            clips.append(ClipPlan(k, r.zone, r.level, len(r.pool), segs, vids.seed, r.cap_s))
        else:
            infeasible.append(k)
    return Assignment(clips, count, sorted(set(infeasible)), tried)
