"""PCB parse isolation: a child that runs past the timeout is killed (422).

The hang and boom targets are resolved by name inside the spawned child, so
they live at module level in this test module.
"""

from __future__ import annotations

import multiprocessing
import time

import pytest

from backend.pcb.render import run_isolated
from backend.penplot.errors import PenPlotError


def hang(data: bytes) -> str:
    """Stand-in for a pathological parse that never finishes."""
    time.sleep(60)
    return "unreachable"


def boom(data: bytes) -> str:
    """Stand-in for a parser that raises."""
    raise ValueError("bad aperture")


def test_parse_past_the_timeout_is_killed_and_returns_422():
    start = time.monotonic()
    with pytest.raises(PenPlotError) as exc:
        run_isolated("backend.tests.test_pcb_isolation:hang", b"x", timeout_s=1.0)
    assert exc.value.status == 422
    assert exc.value.code == "invalid_params"
    assert "could not be parsed within" in exc.value.message
    assert time.monotonic() - start < 20
    assert multiprocessing.active_children() == []


def test_child_exception_returns_422_without_leaking_detail():
    with pytest.raises(PenPlotError) as exc:
        run_isolated("backend.tests.test_pcb_isolation:boom", b"x", timeout_s=20.0)
    assert exc.value.status == 422
    assert exc.value.message == "The Gerber file could not be parsed."
    assert multiprocessing.active_children() == []
