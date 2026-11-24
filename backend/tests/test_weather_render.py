"""Weather artwork composition: layers, text-free output, determinism, budgets, labels."""

from __future__ import annotations

import re

import pytest

from backend.weather import config as C
from backend.weather.extrema import find_extrema
from backend.weather.lettering import Box, choose_slot, lettering
from backend.weather.render import compose_artwork, fit_frame
from backend.weather.streamlines import streamlines

from backend.tests.weather_render_fixtures import fixture_fields, fixture_geo

OPTIONAL = ("isobars", "z500", "isotherms", "markers")


@pytest.fixture(scope="module")
def inputs():
    fields = fixture_fields()
    from backend.tests.weather_render_fixtures import fixture_subset

    sub = fixture_subset()
    return fields, (sub.lat, sub.lon), fixture_geo()


def _compose(inputs, layers, cities=("Budapest",), orientation="landscape"):
    fields, grid, geo = inputs
    needed = {k: v for k, v in fields.items()}
    return compose_artwork(region=C.EUROPE, orientation=orientation, layers=tuple(layers),
                           cities=tuple(cities), geo=geo, grid=grid, fields=needed)


def _groups(svg: str) -> set[str]:
    return set(re.findall(r'data-layer="([a-z0-9]+)"', svg))


def test_default_layers_only(inputs):
    art = _compose(inputs, C.DEFAULT_LAYERS)
    assert _groups(art.svg) == {"coast", "borders", "streamlines", "cities"}


@pytest.mark.parametrize("layer", OPTIONAL)
def test_optional_layer_appears_only_when_asked(inputs, layer):
    assert layer not in _groups(_compose(inputs, C.DEFAULT_LAYERS).svg)
    art = _compose(inputs, (*C.DEFAULT_LAYERS, layer))
    assert _groups(art.svg) == {"coast", "borders", "streamlines", "cities", layer}


def test_no_text_or_image_in_output(inputs):
    art = _compose(inputs, C.LAYER_IDS, cities=tuple(C.CITIES))
    assert "<text" not in art.svg
    assert "<image" not in art.svg
    assert art.svg.startswith("<svg")


def test_deterministic_for_equal_input(inputs):
    a = _compose(inputs, C.LAYER_IDS, cities=tuple(C.CITIES))
    b = _compose(inputs, C.LAYER_IDS, cities=tuple(C.CITIES))
    assert a.svg == b.svg
    assert a.stats == b.stats


def test_stats_minutes_follow_the_pen_speed(inputs):
    art = _compose(inputs, C.LAYER_IDS, cities=tuple(C.CITIES))
    total = art.stats["total"]
    assert total["minutes"] == pytest.approx(total["mm"] / C.PLOT_SPEED_MM_S / 60.0, abs=0.01)
    summed = sum(v["mm"] for v in art.stats["layers"].values())
    assert total["mm"] == pytest.approx(summed, abs=0.5)


def test_streamline_budget_respected(inputs):
    fields, (lat, lon), _geo = inputs
    res = streamlines(fields["u250"], fields["v250"], lat, lon,
                      frame=fit_frame(C.EUROPE, "landscape"))
    assert len(res.lines) <= C.STREAM_MAX_PATHS
    assert sum(line.length_mm for line in res.lines) <= C.STREAM_PAPER_BUDGET_MM


def test_city_label_slots_do_not_overlap():
    frame = fit_frame(C.EUROPE, "landscape")
    taken: list[Box] = []
    slots: list[Box] = []
    for name, (lat, lon) in C.CITIES.items():
        x, y = frame.xy_point(lon, lat)
        r = C.CITY_DOT_MM
        anchor = (x - r, y - r, x + r, y + r)
        dot = Box(*anchor)
        if not dot.inside(frame.box):
            continue
        taken.append(dot)
        slot = choose_slot(lettering(name, C.LABEL_HEIGHT_MM), anchor, taken, frame.box, gap=0.8)
        if slot is not None:
            taken.append(slot)
            slots.append(slot)
    assert len(slots) >= len(C.CITIES) - 2
    for i, a in enumerate(slots):
        for b in slots[i + 1:]:
            assert not a.overlaps(b), (a, b)


def test_markers_are_separated_and_not_duplicated(inputs):
    fields, (lat, lon), _geo = inputs
    marks = find_extrema(fields["mslp_hpa"], lon, lat)
    for i, a in enumerate(marks):
        for b in marks[i + 1:]:
            if a.kind == b.kind:
                from backend.weather.geo import km_between

                assert km_between(a.lon, a.lat, b.lon, b.lat) >= C.MIN_EXTREMA_SEPARATION_KM


def test_attribution_is_the_cc_by_placeholder():
    assert "CC BY 4.0" in C.ATTRIBUTION
    assert "data modified" in C.ATTRIBUTION


def test_frame_stays_on_the_sheet_for_both_orientations():
    for orientation, (w, h) in C.SHEET_MM.items():
        frame = fit_frame(C.EUROPE, orientation)
        b = frame.box
        assert b.x0 >= C.ARTWORK_INSET_MM - 1e-6 and b.x1 <= w - C.ARTWORK_INSET_MM + 1e-6
        assert b.y0 >= C.ARTWORK_INSET_MM - 1e-6 and b.y1 <= h - C.ARTWORK_INSET_MM + 1e-6


def test_closed_ring_keeps_its_shape():
    import numpy as np

    from backend.weather.geo import simplify

    theta = np.linspace(0, 2 * np.pi, 80, endpoint=True)
    ring = [(float(10 + 2 * np.cos(t)), float(50 + 2 * np.sin(t))) for t in theta]
    out = simplify(ring, 0.05)
    assert len(out) > 8
    assert out[0] == out[-1]
    assert len(out) < len(ring)
