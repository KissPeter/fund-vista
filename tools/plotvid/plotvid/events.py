"""Strokes: the intervals between consecutive pen events, labelled.

Pen events are not typed (up vs down), so a stroke between two events may
be a drawn line or a pen-up travel move. The ink check decides: a stroke
is *drawing* when at least ``drawn_min_px`` of new persistent ink appears
over it.

The ink check works at ~10 fps, so it cannot resolve the dense
0.1-0.2 s strokes of detail runs; their labels are noisy. That is
acceptable because cut rules only care about whether *long* strokes were
drawn. *Long* means a duration at or above the ``long_percentile`` of the
source's stroke durations.

If the ink check is off, or too few long strokes read as drawing to
believe it (``min_drawn_share``), every stroke is labelled unknown
(``drawing=None``). Segment rules then treat unknown as "may be drawing",
so any event can cut.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from plotvid.config import StrokesConfig


@dataclass(frozen=True)
class Stroke:
    index: int  # stroke k runs from event k to event k+1
    start_s: float
    end_s: float
    new_ink: float | None  # px at the ink reference width; None = not measured
    drawing: bool | None  # None = unknown (ink unreliable or off)
    long: bool

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def maybe_drawn(self) -> bool:
        return self.drawing is not False


@dataclass(frozen=True)
class StrokeLabels:
    strokes: list[Stroke]
    long_cutoff_s: float
    ink_reliable: bool
    drawn_share_long: float  # share of long strokes read as drawing


def build_strokes(event_times: list[float] | np.ndarray, new_ink: list[float] | None,
                  cfg: StrokesConfig) -> StrokeLabels:
    t = np.asarray(sorted(event_times), dtype=float)
    if len(t) < 2:
        return StrokeLabels([], 0.0, False, 0.0)
    dur = np.diff(t)
    cutoff = float(np.percentile(dur, cfg.long_percentile))
    long = dur >= cutoff
    if new_ink is not None and len(new_ink) != len(dur):
        raise ValueError("new_ink must have one value per stroke")
    drawn = None if new_ink is None else np.asarray(new_ink, dtype=float) >= cfg.drawn_min_px
    share = float(drawn[long].mean()) if drawn is not None and long.any() else 0.0
    reliable = drawn is not None and share >= cfg.min_drawn_share
    strokes = [
        Stroke(
            index=k,
            start_s=float(t[k]),
            end_s=float(t[k + 1]),
            new_ink=None if new_ink is None else float(new_ink[k]),
            drawing=bool(drawn[k]) if reliable else None,
            long=bool(long[k]),
        )
        for k in range(len(dur))
    ]
    return StrokeLabels(strokes, cutoff, reliable, share)


def ink_curve(strokes: list[Stroke]) -> tuple[np.ndarray, np.ndarray]:
    """Monotone cumulative new-ink curve sampled at stroke ends: (times, ink)."""
    if not strokes:
        return np.zeros(0), np.zeros(0)
    times = np.array([strokes[0].start_s] + [s.end_s for s in strokes])
    gains = np.array([0.0] + [max(0.0, s.new_ink or 0.0) for s in strokes])
    return times, np.cumsum(gains)
