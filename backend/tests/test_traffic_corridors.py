"""Corridor-collapse tests (no app, no network).

The pure geometry lives in ``backend/traffic/corridors.py``; the render
wiring is asserted here via ``render_svg``. Everything stays hermetic —
synthetic strokes only.
"""

from __future__ import annotations

import math
import re
import time

from backend.traffic import corridors as C
from backend.traffic import render as R
from backend.traffic.classify import classify_day
from backend.traffic.corridors import corridor_keep
from backend.traffic.filters import Filters

W = 1000.0  # default canvas width -> tol = 2 user units (≈ 0.6 mm on paper)

LAT, LON, ELEV = 47.4369, 19.2556, 495.0
AP = {"lat": LAT, "lon": LON, "elev_ft": ELEV}
BACKGROUND = {"motorways": [], "waterways": [], "water": [], "airfield": []}


def _expected_reps(n: int) -> int:
    """A seed covers itself + up to CORRIDOR_SPLIT joins (retires after that)."""
    return math.ceil(n / (C.CORRIDOR_SPLIT + 1))


def _line(y: float, x_from: float = 200.0, x_to: float = 800.0, wobble=0.0, n=21):
    """A horizontal trace with optional metre-scale wobble (well under tol)."""
    pts = []
    for i in range(n):
        f = i / (n - 1)
        x = x_from + f * (x_to - x_from)
        pts.append((x, y if wobble == 0.0 else y + wobble * (((i * 7) % 5) - 2)))
    return pts


# -- pure geometry ----------------------------------------------------------------

def test_identical_traces_collapse_to_few_lines():
    """The funnel case: 50 same-corridor flights -> ~1 pass per 9 strokes."""
    strokes = [_line(500.0) for _ in range(50)]
    keep = corridor_keep(strokes, W)
    assert len(keep) == _expected_reps(50)
    assert keep == sorted(keep)


def test_jittered_retraces_still_collapse():
    """Real traces wobble by metres — a hair on the plotted sheet."""
    strokes = [_line(500.0, wobble=0.4) for _ in range(24)]
    assert len(corridor_keep(strokes, W)) == _expected_reps(24)


def test_different_corridors_survive():
    """Parallel strokes >tol apart are distinct corridors — nothing merged."""
    strokes = [_line(500.0 + 3.0 * k) for k in range(10)]
    assert corridor_keep(strokes, W) == list(range(10))


def test_crossing_and_branching_traces_survive():
    """A crossing leg or a diverged tail carries different ink -> kept."""
    line = _line(500.0)
    cross = [(502.0 + 0.06 * (k - 10), 400.0 + 10.0 * (k - 10)) for k in range(21)]
    bowed = _line(500.0)[:-6] + [(740.0, 520.0), (800.0, 560.0)]
    assert corridor_keep([line, cross, bowed], W) == [0, 1, 2]


def test_partial_overlap_with_diverging_tail_kept():
    """Half the corridor is shared, then the trace turns away -> its own pass."""
    strokes = [
        _line(500.0),
        _line(500.0, x_to=700.0) + [(700.0, 560.0), (760.0, 640.0)],
    ]
    assert corridor_keep(strokes, W) == [0, 1]


def test_degenerate_and_tiny_strokes_kept():
    one = [(500.0, 500.0), (500.001, 500.0)]
    assert corridor_keep([_line(500.0), one, [(7.0, 7.0)]], W) == [0, 1, 2]


def test_split_retire_keeps_density():
    """A 20-flight funnel keeps ceil(20/9)=3 passes, not 20, not 1."""
    strokes = [_line(500.0) for _ in range(20)]
    assert C.collapse_counts(strokes, W) == _expected_reps(20) == 3


def test_holding_ovals_imperfect_laps_diverge():
    """Identical holding laps merge; wind-drifted ovals keep their passes."""

    def oval(cx=500.0, cy=500.0, r=80.0, phase=0.0, aspect=1.0):
        return [(cx + r * aspect * math.cos(math.radians(a) + phase),
                 cy + r * math.sin(math.radians(a) + phase))
                for a in range(0, 361, 10)]

    same = [oval() for _ in range(12)]
    assert len(corridor_keep(same, W)) == _expected_reps(12)
    drifted = [oval(phase=0.3 * k, aspect=1 + 0.05 * k) for k in range(4)]
    assert corridor_keep(drifted, W) == [0, 1, 2, 3]


def test_dense_funnel_is_fast_enough():
    """Perf sanity: a dense day of 4000 strokes must not stall the render."""
    strokes = [_line(500.0 if k % 4 == 0 else 500.0 + 3.0 * (k % 7), n=41)
               for k in range(4000)]
    t0 = time.monotonic()
    keep = corridor_keep(strokes, W)
    elapsed = time.monotonic() - t0
    assert len(keep) < len(strokes)
    assert elapsed < 15.0, "corridor_keep too slow on a dense funnel: %.2fs" % elapsed


# -- render wiring ----------------------------------------------------------------

def _leg(points, t0=1_000_000.0, step=10.0):
    return [[t0 + i * step, la, lo, alt, 1 if alt == 0 else 0] for i, (la, lo, alt) in enumerate(points)]


def _approach(n=60, start_km=60.0):
    """Straight-in arrival from the NW ending on the field (the funnel)."""
    pts = []
    for i in range(n):
        f = 1 - i / (n - 1)
        d = start_km * f / 111.2
        alt = 0 if i == n - 1 else ELEV + 300 + 9000 * f
        pts.append((LAT + d * 0.7, LON - d * 0.7, alt))
    return pts


def _funnel_moves(n=50):
    flights = [dict(hex="a%d" % i, reg=None, type="A321", desc=None, operator=None,
                    calls=[[0, "WZZ%d" % i]], pts=_leg(_approach()))
               for i in range(n)]
    return classify_day({"airport": AP, "flights": flights})


def test_render_collapse_draws_ratio_but_counts_movement():
    """The SVG shows corridor passes; the count still says 50 arrivals."""
    svg, counts = R.render_svg(_funnel_moves(50), BACKGROUND,
                               (LAT - 0.5, LAT + 0.5, LON - 0.7, LON + 0.7),
                               Filters(t_from=999_000, t_to=999_000 + 86_400), AP)
    assert counts["arrivals"] == 50  # potential/movement count intact
    arrivals = re.search(r'id="traffic-arrivals".*?</g>', svg, re.S).group(0)
    drawn = arrivals.count("<path")
    assert 1 <= drawn < counts["arrivals"]  # the funnel is collapsed
    assert drawn == _expected_reps(50)     # — and it follows the retire rule exactly


def test_render_zero_movements_means_zero():
    """Zero stays zero: collapse never invents or keeps anything on its own."""
    svg, counts = R.render_svg([], BACKGROUND, (47.0, 48.0, 19.0, 20.0),
                               Filters(t_from=0, t_to=86_400), None)
    assert counts == {"arrivals": 0, "departures": 0, "holding": 0, "go_around": 0}
    assert "<path" not in svg
