"""Convert keeps the airport draft's runway layer in its own ``runway-ink`` group,
so the shop can recolour every runway instead of guessing paths from geometry."""
import re

from backend.penplot import pipeline
from backend.penplot.config import Settings
from backend.penplot.schemas import ConvertParams

DRAFT = b"""<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300" viewBox="0 0 200 300">
<g id="osm-taxiways" fill="none" stroke="#000000"><path d="M 10 150 L 190 150"/><path d="M 100 10 L 100 290"/></g>
<g id="runways" fill="none" stroke="#000000"><path d="M 40 20 L 40 280"/><path d="M 44 20 L 44 280"/>
<path d="M 150 40 L 150 260"/><path d="M 154 40 L 154 260"/></g>
<g id="runway-marks" fill="none" stroke="#000000"><path d="M 60 20 L 70 20"/></g>
</svg>"""


def _convert(**kw):
    params = ConvertParams(methods=["contour"], **kw)
    return pipeline.run_convert(
        image_id="a" * 64, image_bytes=DRAFT, is_vector=True, src_w=200, src_h=300,
        params=params, settings=Settings(),
    )


def _groups(svg):
    return {
        (re.search(r'id="([^"]+)"', m.group(1)) or ["", "main"])[1]: len(re.findall(r"<path", m.group(2)))
        for m in re.finditer(r"<g([^>]*)>(.*?)</g>", svg, re.S)
    }


def test_runway_groups_split_out_when_asked():
    g = _groups(_convert(runway_ink_group=True).svg_text)
    assert g["runway-ink"] == 4  # both runways, 2 hatch lines each
    assert g["main"] >= 3  # taxiways + marks stay in the first group


def test_default_output_is_one_group():
    svg = _convert().svg_text
    assert "runway-ink" not in svg and svg.count("<g ") == 1


def _paths(svg):
    return set(re.findall(r'<path d="([^"]+)"', svg))


def test_split_keeps_geometry_registered_with_the_unsplit_sheet():
    """Same layout transform: no stroke moves, none is lost or added."""
    assert _paths(_convert(runway_ink_group=True).svg_text) == _paths(_convert().svg_text)


def test_option_is_ignored_where_it_cannot_apply():
    # Raster-needing method (hatch): no split, same cache key as the default.
    hatch = ConvertParams(methods=["hatch"], runway_ink_group=True)
    assert pipeline.params_hash(hatch, True) == pipeline.params_hash(ConvertParams(methods=["hatch"]), True)
    # Raster input: ignored too.
    assert pipeline.params_hash(ConvertParams(runway_ink_group=True), False) == pipeline.params_hash(
        ConvertParams(), False)


def test_split_has_its_own_cache_key_for_contour_vectors():
    on = ConvertParams(methods=["contour"], runway_ink_group=True)
    off = ConvertParams(methods=["contour"])
    assert pipeline.params_hash(on, True) != pipeline.params_hash(off, True)
