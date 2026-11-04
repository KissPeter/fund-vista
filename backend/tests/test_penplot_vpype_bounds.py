"""N7: the outside-sheet bounds never reach the plotter as geometry.

Runs the real vpype reader. Skipped when vpype is not installed, so the
fund-vista venv and CI stay green without it.
"""

from __future__ import annotations

import io
import re

import pytest

vpype = pytest.importorskip("vpype")

from backend.penplot import compose_sheet as cs  # noqa: E402
from backend.penplot.schemas import ComposeSheetRequest  # noqa: E402

ART = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100">'
    b'<rect x="0" y="0" width="200" height="100" fill="#ff0000"/>'
    b'<circle cx="100" cy="50" r="30" fill="#00ff00"/></svg>'
)
RED = b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100"><rect x="0" y="0" width="200" height="100" fill="#ff0000"/></svg>'
GREEN = b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100"><circle cx="100" cy="50" r="30" fill="#00ff00"/></svg>'
TITLE, SUB = "Title here", "Sub line"


def _sheet(placement: str, art: bytes = ART) -> str:
    req = ComposeSheetRequest.model_validate(
        {
            "svg": art.decode(),
            "layout": {"page": {"frame": True}, "label": {"placement": placement}},
            "lines": [TITLE, SUB],
        }
    )
    return cs.compose_sheet_svg(req, max_svg_bytes=25 * 1024 * 1024)


def _without_bounds_attr(svg: str) -> str:
    return re.sub(r' data-kp-a4-bounds="[^"]*"', "", svg, count=1)


def _with_invisible_rect(svg: str) -> str:
    rect = (
        '<rect id="a4-bounds" x="0" y="0" width="210" height="297" '
        'fill="none" stroke="none"/>'
    )
    return re.sub(r"(<svg[^>]*>)", lambda m: m.group(1) + rect, svg, count=1)


def _stats(svg: str) -> tuple[int, float]:
    doc = vpype.read_multilayer_svg(io.StringIO(svg), quantization=0.01)
    count = sum(len(lc) for lc in doc.layers.values())
    length = sum(lc.length() for lc in doc.layers.values())
    return count, length


@pytest.mark.parametrize("placement", ["below", "above"])
def test_vpype_plots_an_invisible_rect_so_the_rect_is_not_emitted(placement):
    # Documents why the bounds live on the root: vpype reads a fill-none,
    # stroke-none rect as a path, so a rect would change the plot.
    svg = _sheet(placement)
    with_rect = _stats(_with_invisible_rect(svg))
    without = _stats(svg)
    assert with_rect[0] > without[0]
    assert with_rect[1] > without[1]


@pytest.mark.parametrize("placement", ["below", "above"])
def test_emitted_sheet_plots_the_same_as_the_sheet_without_bounds_attr(placement):
    svg = _sheet(placement)
    assert _stats(svg) == _stats(_without_bounds_attr(svg))


@pytest.mark.parametrize("placement", ["below", "above"])
def test_per_ink_sheets_share_the_same_viewbox(placement):
    sheets = [_sheet(placement, art) for art in (RED, GREEN)]
    boxes = {
        re.search(r'width="([^"]+)" height="([^"]+)" viewBox="([^"]+)"', s).groups()
        for s in sheets
    }
    assert len(boxes) == 1
