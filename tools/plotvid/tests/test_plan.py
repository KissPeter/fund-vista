"""Stroke labelling, segment rules, the DP, zone assignment and crop math.

Pure unit tests on synthetic event timelines: no video needed.
"""

from __future__ import annotations

import itertools

import numpy as np
import pytest

from plotvid import assign, events, render, segment
from plotvid.config import SegmentConfig, StrokesConfig, VideosConfig


def pattern(n_blocks: int, t0: float = 1.0, short: float = 0.2, long: float = 1.5,
            shorts: int = 4) -> tuple[list[float], list[float]]:
    """Events for blocks of ``shorts`` short strokes then one long drawn one.

    Returns (event times, new ink per stroke). Short strokes alternate
    drawing/travel; long strokes are drawn."""
    t, times, ink = t0, [t0], []
    for _ in range(n_blocks):
        for i in range(shorts):
            t += short
            times.append(t)
            ink.append(30.0 if i % 2 == 0 else 0.0)
        t += long
        times.append(t)
        ink.append(200.0)
    return times, ink


def test_strokes_labels_from_ink():
    times = [0.0, 0.2, 0.4, 2.0, 2.2, 4.5]
    ink = [25.0, 2.0, 300.0, 0.0, 0.0]
    lab = events.build_strokes(times, ink, StrokesConfig(long_percentile=60, drawn_min_px=20))
    assert [s.drawing for s in lab.strokes] == [True, False, True, False, False]
    assert [s.long for s in lab.strokes] == [False, False, True, False, True]
    assert lab.ink_reliable and lab.drawn_share_long == 0.5
    assert lab.strokes[2].duration_s == pytest.approx(1.6)
    t, cum = events.ink_curve(lab.strokes)
    assert list(t) == times and np.all(np.diff(cum) >= 0) and cum[-1] == 327.0


def test_strokes_unreliable_ink_falls_back_to_unknown():
    times = [0.0, 0.2, 2.0, 2.2, 4.5]
    lab = events.build_strokes(times, [0.0, 1.0, 0.0, 2.0], StrokesConfig())
    assert not lab.ink_reliable
    assert all(s.drawing is None and s.maybe_drawn for s in lab.strokes)
    lab = events.build_strokes(times, None, StrokesConfig())
    assert all(s.drawing is None for s in lab.strokes)


def _labels(times, ink):
    return events.build_strokes(times, ink, StrokesConfig(long_percentile=75)).strokes


def test_candidates_strict_rules_and_bounds():
    strokes = _labels(*pattern(12))
    cfg = SegmentConfig()
    cands = segment.candidates(strokes, cfg, max_level=0)
    assert cands
    for c in cands:
        assert cfg.min_s - 1e-6 <= c.duration_s <= cfg.max_s + 1e-6
        last = strokes[c.last_stroke]
        assert last.long and last.drawing  # ends just after a long drawn stroke
        lead = strokes[c.first_stroke:c.first_stroke + max(cfg.lead_short) + 1]
        n_short = next(i for i, s in enumerate(lead) if s.long)
        assert n_short in cfg.lead_short  # 2-3 short strokes before a long one
        assert c.level == 0
        # Cut lands pre_roll before the first event and post_roll after the last.
        # (clamped to the midpoint with the neighbouring event).
        first, last = strokes[c.first_stroke], strokes[c.last_stroke]
        prev_ev = strokes[c.first_stroke - 1].start_s if c.first_stroke else 0.0
        assert c.start_s == pytest.approx(max(first.start_s - cfg.pre_roll,
                                              (prev_ev + first.start_s) / 2))
        nxt = strokes[c.last_stroke + 1].end_s if c.last_stroke + 1 < len(strokes) else 1e9
        assert c.end_s == pytest.approx(min(last.end_s + cfg.post_roll, (last.end_s + nxt) / 2))


def test_dp_picks_non_overlapping_max_weight():
    strokes = _labels(*pattern(30))
    cands = segment.candidates(strokes, SegmentConfig(), max_level=3)
    chosen = segment.choose(cands)
    assert chosen
    for a, b in itertools.pairwise(chosen):
        assert a.end_s <= b.start_s + 1e-9
    assert all(3 - 1e-6 <= c.duration_s <= 8 + 1e-6 for c in chosen)
    # Optimal against brute force on a small instance.
    small = sorted(cands[:14], key=lambda c: c.start_s)
    best = 0.0
    for mask in range(1 << len(small)):
        pick = [small[i] for i in range(len(small)) if mask >> i & 1]
        if all(not a.overlaps(b) for i, a in enumerate(pick) for b in pick[i + 1:]):
            best = max(best, sum(c.weight for c in pick))
    assert sum(c.weight for c in segment.choose(small)) == pytest.approx(best)


def test_relaxation_used_when_dense():
    # A detail run: only 0.15 s strokes, none long relative to a few huge
    # travels at the end. Strict rules cannot cut inside it.
    times = list(np.round(np.arange(0, 30, 0.15), 3)) + [33.0, 36.0, 39.0]
    ink = [10.0] * (len(times) - 1)
    strokes = events.build_strokes(times, ink, StrokesConfig(long_percentile=99)).strokes
    seg, vids = SegmentConfig(), VideosConfig(count=1, min_total_s=15, target_s=17,
                                              max_total_s=20)
    assert not segment.candidates(strokes, seg, max_level=0, window=(0, 29.0))
    tls = assign.timelines([(strokes, 40.0)])
    res = assign.zone_pool(tls, (0.0, 30.0), seg, vids)
    assert res.level is not None and res.level >= 2
    seg0 = SegmentConfig(max_relax=0)
    assert assign.zone_pool(tls, (0.0, 30.0), seg0, vids).level is None


def _plan(seed=42, count=4):
    strokes = _labels(*pattern(50))
    tls = assign.timelines([(strokes, strokes[-1].end_s + 1)])
    vids = VideosConfig(count=count, seed=seed)
    return tls, assign.assign(tls, SegmentConfig(), vids)


def test_assignment_deterministic_no_reuse_in_zone():
    tls, a = _plan()
    _, b = _plan()
    assert [[(s.start_s, s.end_s) for s in c.segments] for c in a.clips] == \
        [[(s.start_s, s.end_s) for s in c.segments] for c in b.clips]
    assert len(a.clips) == 4 and not a.infeasible_zones
    used = [s for c in a.clips for s in c.segments]
    for i, x in enumerate(used):
        for y in used[i + 1:]:
            assert not x.overlaps(y)
    for c in a.clips:
        assert 15 - 1e-6 <= c.total_s <= 20 + 1e-6
        assert [s.start_s for s in c.segments] == sorted(s.start_s for s in c.segments)
        for s in c.segments:
            g0, g1 = tls[0].to_global(s.start_s), tls[0].to_global(s.end_s)
            assert c.zone[0] - 1e-6 <= g0 and g1 <= c.zone[1] + 1e-6


def test_auto_count_drops_until_feasible():
    strokes = _labels(*pattern(50))  # ~80 s of plotting
    tls = assign.timelines([(strokes, strokes[-1].end_s + 1)])
    res = assign.assign(tls, SegmentConfig(), VideosConfig(count="auto", max_count=7))
    assert res.count < 7 and res.tried[7]
    assert not res.infeasible_zones and len(res.clips) == res.count


def test_two_sources_never_mix_in_a_segment():
    s1 = _labels(*pattern(25))
    s2 = _labels(*pattern(25, t0=0.5))
    tls = assign.timelines([(s1, s1[-1].end_s + 1), (s2, s2[-1].end_s + 1)])
    res = assign.assign(tls, SegmentConfig(), VideosConfig(count=4))
    assert {s.source for c in res.clips for s in c.segments} == {0, 1}
    assert tls[1].offset_s == pytest.approx(tls[0].span_s)


@pytest.mark.parametrize("w, h, expect", [
    (576, 1024, (576, 1024, 0, 0)),  # exact 9:16
    (576, 1280, (576, 1024, 0, 128)),  # too tall: top and bottom
    (1920, 1080, (606, 1080, 657, 0)),  # too wide: left and right
    (1080, 1920, (1080, 1920, 0, 0)),
    (720, 1000, (562, 1000, 79, 0)),  # slightly wide, even sides
])
def test_crop_math(w, h, expect):
    cw, ch, x, y = render.crop_9_16(w, h)
    assert (cw, ch, x, y) == expect
    assert cw % 2 == 0 and ch % 2 == 0 and cw <= w and ch <= h
    assert abs(cw / ch - 9 / 16) < 0.01


def test_output_size_never_upscales():
    assert render.output_size([(576, 1024), (1080, 1920)]) == (576, 1024)
    assert render.nice_fps(339487 / 11311) == 30.0
    assert render.nice_fps(60.0) == 60.0
    assert render.nice_fps(17.0) == 17.0
