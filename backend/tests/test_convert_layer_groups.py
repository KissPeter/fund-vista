"""Convert keeps each traffic layer group (``traffic-*``) as its own Inkscape
layer, so the downloaded finished SVG is organised per layer, not one flat group."""
import re

from backend.penplot import pipeline
from backend.penplot.config import Settings
from backend.penplot.schemas import ConvertParams

DRAFT = b"""<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" width="300" height="200" viewBox="0 0 300 200">
<g id="traffic-country" inkscape:groupmode="layer" fill="none" stroke="#000"><path d="M 10 10 L 290 10"/></g>
<g id="traffic-arrivals" inkscape:groupmode="layer" fill="none" stroke="#000"><path d="M 20 20 L 250 180"/><path d="M 30 20 L 260 180"/></g>
<g id="traffic-departures" inkscape:groupmode="layer" fill="none" stroke="#000"><path d="M 250 20 L 20 180"/></g>
<g id="traffic-holding" inkscape:groupmode="layer" fill="none" stroke="#000"></g>
<g id="traffic-go_around" inkscape:groupmode="layer" fill="none" stroke="#000"><path d="M 100 100 L 200 120"/></g>
</svg>"""


def _convert(**kw):
    return pipeline.run_convert(
        image_id="b" * 64, image_bytes=DRAFT, is_vector=True, src_w=300, src_h=200,
        params=ConvertParams(methods=["contour"], **kw), settings=Settings(),
    )


def _layers(svg):
    return {
        m.group(1): len(re.findall(r"<path", m.group(2)))
        for m in re.finditer(r'<g[^>]* id="([^"]+)"[^>]*>(.*?)</g>', svg, re.S)
    }


def _paths(svg):
    return set(re.findall(r'<path d="([^"]+)"', svg))


def test_every_layer_group_is_kept_empty_ones_dropped():
    svg = _convert(layer_groups=True).svg_text
    got = _layers(svg)
    assert set(got) == {"traffic-country", "traffic-arrivals", "traffic-departures", "traffic-go_around"}
    assert got["traffic-arrivals"] >= 2
    assert 'inkscape:groupmode="layer"' in svg and 'inkscape:label="Go around"' in svg
    assert 'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"' in svg


def test_default_output_is_still_one_group():
    svg = _convert().svg_text
    assert "traffic-" not in svg and svg.count("<g ") == 1 and "inkscape" not in svg


def test_geometry_registered_with_the_flat_sheet():
    assert _paths(_convert(layer_groups=True).svg_text) == _paths(_convert().svg_text)


def test_option_ignored_where_it_cannot_apply_and_has_own_key():
    hatch = ConvertParams(methods=["hatch"], layer_groups=True)
    assert pipeline.params_hash(hatch, True) == pipeline.params_hash(ConvertParams(methods=["hatch"]), True)
    assert pipeline.params_hash(ConvertParams(layer_groups=True), False) == pipeline.params_hash(
        ConvertParams(), False)
    on = ConvertParams(methods=["contour"], layer_groups=True)
    assert pipeline.params_hash(on, True) != pipeline.params_hash(ConvertParams(methods=["contour"]), True)


# The shop's 3D city view plot: two top-level groups, a map (water + roads)
# and a buildings group, each with its own ink after convert.
CITY = b"""<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" viewBox="0 0 210 297">
<g id="map" fill="none"><path d="M 20 20 L 190 20"/><path d="M 20 40 L 120 40"/></g>
<g id="buildings" fill="none"><path d="M 60 100 L 120 100 L 120 160 L 60 160 Z"/><path d="M 130 200 L 170 200"/></g>
</svg>"""


def _city(**kw):
    return pipeline.run_convert(
        image_id="c" * 64, image_bytes=CITY, is_vector=True, src_w=210, src_h=297,
        params=ConvertParams(methods=["contour"], **kw), settings=Settings(),
    )


def test_city_map_and_buildings_stay_separate_layers_with_stats():
    res = _city(layer_groups=True)
    got = _layers(res.svg_text)
    assert set(got) == {"map", "buildings"}
    assert got["buildings"] >= 2 and got["map"] >= 2
    assert 'inkscape:label="Buildings"' in res.svg_text
    assert [layer.id for layer in res.stats.layers] == ["map", "buildings"]
    total = sum(layer.pen_down_mm for layer in res.stats.layers)
    assert abs(total - res.stats.pen_down_mm) < 0.05
    assert sum(layer.strokes for layer in res.stats.layers) == res.stats.strokes


def test_city_groups_stay_flat_by_default_and_stats_have_no_layers():
    res = _city()
    assert "inkscape" not in res.svg_text
    assert res.stats.layers is None
