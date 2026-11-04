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


def test_split_long_gap_into_equal_pieces():
    times, flags = events.split_long_gaps([0.0, 69.0], StrokesConfig())
    assert len(times) == 11 and times[0] == 0.0 and times[-1] == 69.0  # 10 pieces of 6.9 s
    assert [b - a for a, b in itertools.pairwise(times)] == pytest.approx([6.9] * 10)
    assert flags == [False] + [True] * 9 + [False]


def test_split_leaves_gaps_at_or_under_threshold_alone():
    cfg = StrokesConfig()
    assert events.split_long_gaps([0.0, 20.0], cfg) == ([0.0, 20.0], [False, False])
    assert events.split_long_gaps([0.0, 19.0], cfg) == ([0.0, 19.0], [False, False])


def test_split_off_when_none():
    times, flags = events.split_long_gaps([0.0, 69.0], StrokesConfig(split_over_s=None))
    assert times == [0.0, 69.0] and flags == [False, False]


def test_split_lead_in_with_duration_cuts_from_start():
    times, flags = events.split_long_gaps([36.6, 52.9, 54.6], StrokesConfig(), duration_s=56.0)
    assert times[0] == 0.0 and flags[0] is True  # lead-in 36.6 s -> 6 pieces
    assert [t for t, f in zip(times, flags) if not f] == [36.6, 52.9, 54.6]
    assert [b - a for a, b in itertools.pairwise(times[:7])] == pytest.approx([6.1] * 6)


def test_split_tail_with_duration_cuts_to_end():
    times, flags = events.split_long_gaps([0.0, 1.0], StrokesConfig(), duration_s=40.0)
    assert times[-1] == 40.0 and flags[-1] is True
    assert [t for t, f in zip(times, flags) if not f] == [0.0, 1.0]


def test_split_no_events_is_one_gap_over_duration():
    times, flags = events.split_long_gaps([], StrokesConfig(), duration_s=50.0)
    assert times[0] == 0.0 and times[-1] == 50.0 and len(times) == 9  # 8 pieces of 6.25 s
    assert flags == [True] + [True] * 7 + [True]


def test_split_no_events_short_source_untouched():
    assert events.split_long_gaps([], StrokesConfig(), duration_s=15.0) == ([], [])


def test_split_edges_need_duration():
    times, flags = events.split_long_gaps([36.6, 52.9], StrokesConfig())
    assert times == [36.6, 52.9] and flags == [False, False]


def test_split_keeps_original_events_and_flags_only_inserted():
    original = [0.0, 1.0, 70.0, 71.0, 200.0]
    times, flags = events.split_long_gaps(original, StrokesConfig())
    assert times == sorted(times)
    assert [t for t, f in zip(times, flags) if not f] == original
    assert sum(flags) == 9 + 18  # 69 s -> 10 pieces, 129 s -> 19 pieces
    assert all(a < b for a, b in itertools.pairwise(times))


def test_synthetic_flag_marks_stroke_starting_at_piece():
    cfg = StrokesConfig()
    times, flags = events.split_long_gaps([0.0, 30.0, 31.0], cfg)
    lab = events.build_strokes(times, None, cfg, flags)
    assert [s.synthetic_start for s in lab.strokes] == flags[:-1]
    assert not any(s.synthetic_start for s in events.build_strokes(times, None, cfg).strokes)


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


def test_split_long_stroke_gives_candidates_inside_it():
    # A 60 s line with no pen-up move, between normal blocks. Short strokes
    # travel; the long strokes and the line are drawn.
    head, _ = pattern(4)
    t_line = head[-1]
    tail, _ = pattern(4, t0=t_line + 60.0)
    raw = head + tail
    ink = [200.0 if b - a > 1.0 else 0.0 for a, b in itertools.pairwise(raw)]
    window = (t_line, t_line + 60.0)

    unsplit = events.build_strokes(raw, ink, StrokesConfig(split_over_s=None)).strokes
    assert not segment.candidates(unsplit, SegmentConfig(), max_level=3, window=window)

    times, flags = events.split_long_gaps(raw, StrokesConfig())
    ink = [200.0 if b - a > 1.0 else 0.0 for a, b in itertools.pairwise(times)]
    split = events.build_strokes(times, ink, StrokesConfig(), flags).strokes
    cands = segment.candidates(split, SegmentConfig(), max_level=3, window=window)
    assert cands
    for c in cands:
        assert 3 - 1e-6 <= c.duration_s <= 8 + 1e-6
        assert window[0] - 1e-6 <= c.start_s and c.end_s <= window[1] + 1e-6


BUDGET = assign.Budget(ending_s=5.0, join_s=0.5)
VIDS = VideosConfig(count=2, parts=3, seed=42)


def _timeline(blocks=70, sources=1):
    per = []
    for k in range(sources):
        st = _labels(*pattern(blocks, t0=1.0 + 0.5 * k))
        per.append((st, st[-1].end_s + 1.0))
    return assign.timelines(per)


def test_relaxation_used_when_dense():
    # A detail run: only 0.15 s strokes, none long relative to a few huge
    # travels at the end. Strict rules cannot cut inside it, so a clip that
    # needs that footage only becomes feasible at a relaxed level.
    times = list(np.round(np.arange(0, 100, 0.15), 3)) + [104.0, 108.0, 112.0]
    ink = [10.0] * (len(times) - 1)
    strokes = events.build_strokes(times, ink, StrokesConfig(long_percentile=100)).strokes
    seg = SegmentConfig()
    assert not segment.candidates(strokes, seg, max_level=0, window=(0, 99.0))
    tls = assign.timelines([(strokes, 113.0)])
    vids = VideosConfig(count=1, parts=3, seed=1)
    res = assign.assign(tls, seg, vids, BUDGET)
    assert res.clips and res.level >= 2
    assert max(s.level for s in res.clips[0].segments) >= 2  # recorded per segment
    assert not assign.assign(tls, SegmentConfig(max_relax=0), vids, BUDGET).clips
    # No segment-length cap: footage per clip comes from varied lengths.
    assert not hasattr(assign.ClipPlan, "cap_s")


def _flat(a):
    return [[(s.source, s.start_s, s.end_s) for s in c.segments] for c in a.clips]


def test_story_assignment_every_clip_covers_all_parts():
    tls = _timeline(blocks=40, sources=3)  # a file is a part
    seg = SegmentConfig()
    a = assign.assign(tls, seg, VIDS, BUDGET)
    assert len(a.clips) == 2 and a.level is not None
    for c in a.clips:
        parts = [s.part for s in c.segments]
        # Invariant: first segment from the first part, last from the last.
        assert parts[0] == 0 and parts[-1] == 2
        assert assign.story_ok(c.segments, 3)
        assert parts == sorted(parts) and set(parts) == {0, 1, 2}
        assert [(s.source, s.start_s) for s in c.segments] == \
            sorted((s.source, s.start_s) for s in c.segments)  # chronological
        for s in c.segments:  # a segment lies wholly inside its part
            lo, hi = tls[s.source].to_global(s.start_s), tls[s.source].to_global(s.end_s)
            assert a.parts[s.part][0] - 1e-6 <= lo and hi <= a.parts[s.part][1] + 1e-6
    used = [s for c in a.clips for s in c.segments]
    for i, x in enumerate(used):
        for y in used[i + 1:]:
            assert not x.overlaps(y)  # footage never reused


def test_story_assignment_deterministic_and_seeded():
    tls = _timeline()
    a = assign.assign(tls, SegmentConfig(), VIDS, BUDGET)
    b = assign.assign(tls, SegmentConfig(), VIDS, BUDGET)
    c = assign.assign(tls, SegmentConfig(), VideosConfig(count=2, parts=3, seed=7), BUDGET)
    assert _flat(a) == _flat(b)
    assert _flat(a) != _flat(c)


def test_clip_totals_in_window_with_crossfades_and_cuts():
    tls = _timeline()
    for join in (0.5, 0.0):
        budget = assign.Budget(5.0, join)
        a = assign.assign(tls, SegmentConfig(), VIDS, budget)
        assert a.clips
        for c in a.clips:
            n = len(c.segments)
            assert c.footage_s == pytest.approx(sum(s.duration_s for s in c.segments))
            expect = c.footage_s + 5.0 - join * (n + 1)
            assert c.length_s == pytest.approx(expect)
            assert 30 - 1e-6 <= c.length_s <= 40 + 1e-6


def test_auto_count_maximal_and_never_below_minimum():
    tls = _timeline()  # ~160 s of plotting
    res = assign.assign(tls, SegmentConfig(), VideosConfig(count="auto", max_count=8), BUDGET)
    assert res.count >= 2 and res.tried[8] is None
    assert len(res.clips) == res.count
    assert all(c.length_s >= 30 - 1e-6 for c in res.clips)
    # One more clip is not feasible, and the search says so.
    assert res.tried.get(res.count + 1, None) is None
    # An impossible minimum plans nothing instead of shortening clips.
    none = assign.assign(tls, SegmentConfig(), VideosConfig(count=20, max_count=20), BUDGET)
    assert not none.clips and none.count == 0


def test_final_segment_runs_to_source_end():
    strokes = _labels(*pattern(12))
    dur = strokes[-1].end_s + 2.0  # the pen travels home after the last event
    seg = SegmentConfig()
    cands = segment.candidates(strokes, seg, 0, 3, dur, tail=True)
    finals = [c for c in cands if c.final]
    assert finals and all(c.end_s == dur for c in finals)
    assert any(c.duration_s > seg.max_s for c in finals)
    assert all(c.duration_s <= seg.max_s + seg.final_extra_s + 1e-6 for c in finals)
    assert all(c.duration_s <= seg.max_s + 1e-6 for c in cands if not c.final)
    # The timeline extends to the source end, and the last part's final
    # segment is pinned to the last clip.
    tls = assign.timelines([(strokes, dur)])
    assert tls[0].span_s == pytest.approx(dur - strokes[0].start_s)
    big = _timeline()
    a = assign.assign(big, SegmentConfig(), VIDS, BUDGET)
    ends = [(i, s) for i, c in enumerate(a.clips) for s in c.segments if s.final]
    assert len(ends) == 1 and ends[0][0] == len(a.clips) - 1
    assert ends[0][1].end_s == big[0].duration_s


def test_several_sources_each_file_is_one_part():
    tls = _timeline(blocks=40, sources=2)
    res = assign.assign(tls, SegmentConfig(), VideosConfig(count=2, parts=3), BUDGET)
    assert res.clips
    # A new part starts where a file starts: 2 files = 2 parts.
    assert len(res.parts) == 2
    for c in res.clips:
        assert assign.story_ok(c.segments, 2)
        assert [s.source for s in c.segments] == sorted(s.source for s in c.segments)


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


def test_manifest_records_story_invariant():
    from pathlib import Path

    from plotvid import manifest
    tls = _timeline(blocks=40, sources=3)
    a = assign.assign(tls, SegmentConfig(), VIDS, BUDGET)
    assert a.clips
    for c in a.clips:
        m = manifest.clip_manifest("p", c, [Path(f"x{i}.mp4") for i in range(3)],
                                   [tl.strokes for tl in tls],
                                   {"path": None, "seconds": 2.5}, a.parts)
        assert m["story"] == {"parts": 3, "first_segment_part": 0, "last_segment_part": 2,
                              "ok": True}
        assert m["segments"][0]["part"] == 0 and m["segments"][-1]["part"] == 2
        assert all("relax_level" in s for s in m["segments"])
        assert m["length_s"] == pytest.approx(c.length_s)
