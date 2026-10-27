"""Unit tests for the plot-time calculator (pipeline.estimate_plot_time_s).

The feedrates are measured machine constants (schemas.DRAW_SPEED_MM_MIN /
TRAVEL_SPEED_MM_MIN, mm/min) and are NOT request parameters (2026-10-27), so
the degenerate-input regression lives here instead of over HTTP: the old
test_penplot_http test could no longer express a subnormal speed once the
fields left the API. It keeps the Schemathesis ST101 finding (2026-10-20)
covered — an inf estimate must be dropped by the caller, never reach the
JSON response render.
"""

import math

import pytest

from backend.penplot import pipeline


def test_machine_feedrates():
    assert pipeline.DRAW_SPEED_MM_MIN == 2000.0
    assert pipeline.TRAVEL_SPEED_MM_MIN == 8000.0


def test_estimate_uses_measured_feedrates():
    # 60 mm pen-down at 2000 mm/min = 1.8 s; 60 mm pen-up at 8000 mm/min = 0.45 s.
    est = pipeline.estimate_plot_time_s(60.0, 60.0, strokes=0, pen_lift_s=0.0)
    assert est == pytest.approx(2.25)


def test_estimate_counts_pen_lifts():
    est = pipeline.estimate_plot_time_s(0.0, 0.0, strokes=10, pen_lift_s=0.3)
    assert est == pytest.approx(3.0)


def test_estimate_degenerate_speed_is_inf(monkeypatch):
    # Regression guard (ST101): a degenerate speed inflates its term to inf;
    # run_convert turns that into estimated_time_s=0 + a warning, never a 500.
    monkeypatch.setattr(pipeline, "TRAVEL_SPEED_MM_MIN", 5e-324)
    est = pipeline.estimate_plot_time_s(0.0, 60.0, strokes=0, pen_lift_s=0.0)
    assert math.isinf(est)
