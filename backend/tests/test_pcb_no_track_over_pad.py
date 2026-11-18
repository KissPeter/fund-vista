"""No plotted track runs inside a pad, on the real ESP32DIV2 board, on every path.

Regression guard for "traces go to the middle of the pads": the SVG a buyer sees
(compose, HTTP render, and the cached repeat of it) is measured, not the shapes.
Overlap is the track length inside a pad outline in the plotted sheet.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from backend.pcb.layers import board_shapes
from backend.pcb.pad_edge import _inside
from backend.pcb.schemas import PcbRenderParams
from backend.pcb.sheet import compose_sheet

FIXTURE = Path(__file__).parent / "fixtures" / "ESP32DIV2-pcb.GTL"
#: Sheet units are mm; a track may touch a pad edge but not enter it by more than this.
TOLERANCE_MM = 0.01

_PATHS = re.compile(r'<path d="([^"]+)"')
_POINT = re.compile(r"[ML]([-\d.]+) ([-\d.]+)")


def _group(svg: str, layer: str) -> list[list[tuple[float, float]]]:
    body = svg[svg.index(f'id="pcb-{layer}"'):].split("</g>")[0]
    return [[(float(x), float(y)) for x, y in _POINT.findall(d)] for d in _PATHS.findall(body)]


def track_length_inside_pads(svg: str) -> float:
    """Length (mm) of plotted track lying inside a pad outline (pad shrunk 5 % to ignore the edge)."""
    pads = []
    for outline in _group(svg, "pads"):
        cx = sum(x for x, _ in outline) / len(outline)
        cy = sum(y for _, y in outline) / len(outline)
        shrunk = [(cx + (x - cx) * 0.95, cy + (y - cy) * 0.95) for x, y in outline]
        box = (min(x for x, _ in shrunk), max(x for x, _ in shrunk),
               min(y for _, y in shrunk), max(y for _, y in shrunk))
        pads.append((shrunk, box))
    total = 0.0
    for track in _group(svg, "tracks"):
        for a, b in zip(track, track[1:]):
            n = 40
            length = ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
            for i in range(n):
                q = (a[0] + (b[0] - a[0]) * (i + 0.5) / n, a[1] + (b[1] - a[1]) * (i + 0.5) / n)
                if any(bx[0] <= q[0] <= bx[1] and bx[2] <= q[1] <= bx[3] and _inside(q, poly)
                       for poly, bx in pads):
                    total += length / n
    return total


def _params(**extra) -> dict:
    return {
        "paper": "white", "pen": "black",
        "layers": {"pads": {"enabled": True, "ink": extra.pop("pads", "blue")},
                   "tracks": {"enabled": True, "ink": extra.pop("tracks", "black")}},
        **extra,
    }


def test_the_overlap_measure_sees_an_untrimmed_sheet(monkeypatch):
    """The guard is only worth something if it fails without the trim."""
    import backend.pcb.sheet as sheet

    monkeypatch.setattr(sheet, "trim_tracks_at_pads", lambda shapes: shapes)
    shapes = board_shapes(FIXTURE.read_bytes())
    svg = compose_sheet(shapes, PcbRenderParams.model_validate(_params())).svg
    assert track_length_inside_pads(svg) > 50.0


@pytest.mark.parametrize("variant", [
    {},
    {"pads": "auto", "tracks": "auto"},
    {"pads": "black", "tracks": "blue"},
    {"mirror": True},
    {"lines": ["Title", "Subtitle"], "layout": {"page": {}, "label": {}}},
], ids=["blue-black", "auto", "black-blue", "mirror", "titled"])
def test_composed_sheet_has_no_track_inside_a_pad(variant):
    shapes = board_shapes(FIXTURE.read_bytes())
    svg = compose_sheet(shapes, PcbRenderParams.model_validate(_params(**dict(variant)))).svg
    assert track_length_inside_pads(svg) <= TOLERANCE_MM


def test_http_render_and_its_cached_repeat_have_no_track_inside_a_pad(http_client):
    data = {"params": json.dumps(_params())}
    files = {"file": ("ESP32DIV2-pcb.GTL", FIXTURE.read_bytes(), "application/octet-stream")}
    first = http_client.post("/v1/pcb/render", files=files, data=data)
    assert first.status_code == 200, first.text
    again = http_client.post("/v1/pcb/render", files=files, data=data)
    assert again.status_code == 200 and again.json()["cache_hit"] is True
    served = http_client.get(again.json()["svg_url"])
    for svg in (first.json()["svg"], again.json()["svg"], served.text):
        assert track_length_inside_pads(svg) <= TOLERANCE_MM
