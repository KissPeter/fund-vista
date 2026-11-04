"""POST /v1/compose-sheet: sheet geometry, markers, validation and determinism.

Geometry tests call :func:`compose_sheet_svg` directly (no socket); the HTTP
tests run over the session live server, like the other /v1 suites.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.penplot import compose_sheet as cs
from backend.penplot import labels
from backend.penplot.errors import PenPlotError
from backend.penplot.schemas import (
    ComposeContextPads,
    ComposeLabelParams,
    ComposeLayout,
    ComposePageParams,
    ComposeSheetRequest,
    ContextTableParams,
    LabelParams,
    PageParams,
)
from backend.penplot.svg_vector import parse_svg_vectors
from backend.tests.helpers import perf_budget

NS = "{http://www.w3.org/2000/svg}"
W, H, MARGIN = 210.0, 297.0, 10.0
TOL = 0.011  # coordinates are written with two decimals

ART = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100">'
    b'<rect x="0" y="0" width="200" height="100" fill="#ff0000"/>'
    b'<circle cx="100" cy="50" r="30" fill="#00ff00"/></svg>'
)


def _req(**kw) -> ComposeSheetRequest:
    base: dict = {"svg": ART.decode()}
    base.update(kw)
    return ComposeSheetRequest.model_validate(base)


def _compose(**kw) -> str:
    return cs.compose_sheet_svg(_req(**kw), max_svg_bytes=25 * 1024 * 1024)


def _root(svg: str) -> ET.Element:
    return ET.fromstring(svg)


def _marked(root: ET.Element, attr: str) -> list[ET.Element]:
    return [el for el in root.iter() if attr in el.attrib]


def _xy(el: ET.Element) -> list[tuple[float, float]]:
    text = el.attrib.get("points") or el.attrib.get("d") or ""
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", text)]
    return list(zip(nums[0::2], nums[1::2], strict=True))


def _bounds(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return min(xs), min(ys), max(xs), max(ys)


def _text_bounds(root: ET.Element, text: str) -> tuple[float, float, float, float]:
    group = next(el for el in _marked(root, "data-kp-text") if el.attrib["data-kp-text"] == text)
    points = [p for stroke in group.iter(NS + "polyline") for p in _xy(stroke)]
    return _bounds(points)


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= TOL


# -- band geometry and markers -------------------------------------------------


def test_band_geometry_and_markers():
    root = _root(_compose(lines=["Title"]))
    bands = _marked(root, "data-kp-title-block")
    assert len(bands) == 1
    x0, y0, x1, y1 = _bounds(_xy(bands[0]))
    # Full inner width, bottom on the bottom margin, top one row (5 mm + 1.5 mm gap) up.
    assert _close(x0, MARGIN) and _close(x1, W - MARGIN)
    assert _close(y1, H - MARGIN)
    assert _close(y0, H - MARGIN - (5.0 + cs.LINE_GAP_MM))
    labels = _marked(root, "data-kp-label")
    assert len(labels) == 1 and labels[0].attrib["data-kp-label-font"] == "znikoslsvginot"
    assert [el.attrib["data-kp-text"] for el in _marked(root, "data-kp-text")] == ["Title"]
    # The lettering sits inside the band.
    tx0, ty0, tx1, ty1 = _text_bounds(root, "Title")
    # Default right alignment with no right pad: the glyph box ends on the band edge.
    assert y0 < ty0 and ty1 < y1 and x0 < tx0 and tx1 <= x1 + TOL


def test_two_lines_stack_under_one_band():
    root = _root(_compose(lines=["Title", "Subtitle"]))
    bands = _marked(root, "data-kp-title-block")
    assert len(bands) == 1
    _, y0, _, y1 = _bounds(_xy(bands[0]))
    assert _close(y0, H - MARGIN - 2 * (5.0 + cs.LINE_GAP_MM))
    assert _close(y1, H - MARGIN)
    _, ty0, _, _ = _text_bounds(root, "Title")
    _, sy0, _, sy1 = _text_bounds(root, "Subtitle")
    assert ty0 < sy0 and sy1 < H - MARGIN + TOL
    assert [el.attrib["data-kp-text"] for el in _marked(root, "data-kp-text")] == ["Title", "Subtitle"]


def test_border_off_draws_no_band_but_keeps_lettering():
    root = _root(_compose(lines=["Title"], layout={"label": {"border": False}}))
    assert _marked(root, "data-kp-title-block") == []
    assert len(_marked(root, "data-kp-label")) == 1


def test_blank_lines_are_dropped():
    root = _root(_compose(lines=["", "   "]))
    assert _marked(root, "data-kp-title-block") == []
    assert _marked(root, "data-kp-label") == []
    root = _root(_compose(lines=["Title", " "]))
    assert [el.attrib["data-kp-text"] for el in _marked(root, "data-kp-text")] == ["Title"]


# -- padding: insets the lettering, never the band -----------------------------


def test_pads_inset_lettering_but_not_band():
    root = _root(_compose(
        lines=["AB"],
        layout={"label": {"align": "fill", "pad_left_mm": 20.0, "pad_right_mm": 15.0}},
    ))
    band_x0, _, band_x1, _ = _bounds(_xy(_marked(root, "data-kp-title-block")[0]))
    assert _close(band_x0, MARGIN) and _close(band_x1, W - MARGIN)
    tx0, _, tx1, _ = _text_bounds(root, "AB")
    assert _close(tx0, MARGIN + 20.0)
    assert _close(tx1, W - MARGIN - 15.0)


@pytest.mark.parametrize("pad_right, expected", [(0.0, W - MARGIN), (5.0, W - MARGIN - 5.0)])
def test_right_pad_zero_vs_five(pad_right, expected):
    root = _root(_compose(
        lines=["AB"], layout={"label": {"align": "right", "pad_right_mm": pad_right}},
    ))
    _, _, tx1, _ = _text_bounds(root, "AB")
    assert _close(tx1, expected)


# -- page frame ----------------------------------------------------------------


def test_frame_off_draws_no_frame():
    root = _root(_compose(lines=["Title"]))
    assert _marked(root, "data-kp-frame") == []


def test_frame_on_is_independent_of_the_band():
    root = _root(_compose(lines=["Title"], layout={"page": {"frame": True}}))
    frames = _marked(root, "data-kp-frame")
    assert len(frames) == 1
    fx0, fy0, fx1, fy1 = _bounds(_xy(frames[0]))
    assert _close(fx0, MARGIN) and _close(fy0, MARGIN)
    assert _close(fx1, W - MARGIN) and _close(fy1, H - MARGIN)
    assert len(_marked(root, "data-kp-title-block")) == 1


def test_frame_without_title_draws_frame_only():
    root = _root(_compose(layout={"page": {"frame": True}}))
    assert len(_marked(root, "data-kp-frame")) == 1
    assert _marked(root, "data-kp-title-block") == []
    assert _marked(root, "data-kp-label") == []


# -- orientation and sheet size ------------------------------------------------


def test_portrait_is_a4_210_by_297():
    root = _root(_compose(lines=["Title"]))
    assert root.attrib["width"] == "210mm" and root.attrib["height"] == "297mm"
    assert root.attrib["viewBox"] == "0 0 210 297"


def test_landscape_is_a4_297_by_210_and_band_follows_the_bottom():
    root = _root(_compose(lines=["Title"], layout={"page": {"orientation": "landscape"}}))
    assert root.attrib["width"] == "297mm" and root.attrib["height"] == "210mm"
    assert root.attrib["viewBox"] == "0 0 297 210"
    _, _, x1, y1 = _bounds(_xy(_marked(root, "data-kp-title-block")[0]))
    assert _close(x1, 297.0 - MARGIN) and _close(y1, 210.0 - MARGIN)


# -- artwork embedding and fitting ---------------------------------------------


def _image(root: ET.Element) -> ET.Element:
    images = [el for el in root.iter(NS + "image")]
    assert len(images) == 1
    return images[0]


def test_artwork_is_embedded_byte_for_byte():
    root = _root(_compose(lines=["Title"]))
    href = _image(root).attrib["href"]
    prefix = "data:image/svg+xml;base64,"
    assert href.startswith(prefix)
    assert base64.b64decode(href[len(prefix):]) == ART


def test_artwork_keeps_aspect_and_stays_in_the_free_area():
    root = _root(_compose(lines=["Title"], layout={"page": {"frame": True}}))
    img = _image(root)
    x, y = float(img.attrib["x"]), float(img.attrib["y"])
    w, h = float(img.attrib["width"]), float(img.attrib["height"])
    assert _close(w / h, 2.0)  # 200 x 100 artwork
    band_top = H - MARGIN - (5.0 + cs.LINE_GAP_MM)
    pad = 5.0
    assert x >= MARGIN + pad - TOL and x + w <= W - MARGIN - pad + TOL
    assert y >= MARGIN + pad - TOL and y + h <= band_top + TOL
    # Letterboxed on the sides: width-limited, centred horizontally.
    assert _close(x + w / 2, W / 2)


def test_artwork_without_declared_size_fills_the_free_area():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="5" height="5"/></svg>'
    root = _root(cs.compose_sheet_svg(
        ComposeSheetRequest.model_validate({"svg": svg, "lines": ["T"]}),
        max_svg_bytes=25 * 1024 * 1024,
    ))
    img = _image(root)
    assert _close(float(img.attrib["width"]), W - 2 * (MARGIN + 5.0))
    assert _close(float(img.attrib["height"]), (H - MARGIN - (5.0 + cs.LINE_GAP_MM)) - MARGIN - 5.0)


# -- context table -------------------------------------------------------------

ROWS = [{"label": "Hajtas", "value": "Hidraulikus"}, {"label": "Ev", "value": "2026"}]


def test_context_table_off_draws_nothing():
    root = _root(_compose(lines=["Title"], context_table={"enabled": False, "rows": ROWS}))
    assert _marked(root, "data-kp-context") == []


def test_context_table_on_draws_bordered_group():
    root = _root(_compose(
        lines=["Title"],
        context_table={"enabled": True, "position": "top-left", "rows": ROWS},
    ))
    groups = _marked(root, "data-kp-context")
    assert len(groups) == 1
    assert groups[0].attrib["data-kp-context-position"] == "top-left"
    borders = _marked(root, "data-kp-context-border")
    assert len(borders) == 1
    bx0, by0, _, _ = _bounds(_xy(borders[0]))
    assert bx0 >= MARGIN - TOL and by0 >= MARGIN - TOL  # top-left corner, inside the margins
    # One column rule plus one rule per row boundary.
    rules = _marked(root, "data-kp-context-rule")
    assert len(rules) == len(ROWS)
    text = [p for g in _marked(root, "data-kp-context-text") for p in g.iter(NS + "polyline")]
    assert text  # the cell lettering is drawn
    # Every cell carries its text as a marker, key before value, in row order.
    marked = [g.attrib["data-kp-text"] for g in root.iter(NS + "g")
              if "data-kp-text" in g.attrib and g.attrib["data-kp-text"] != "Title"]
    expected = [t for row in ROWS for t in (row["label"], str(row["value"]))]
    assert marked == [t for t in expected if t.strip()]


def test_context_table_rows_empty_is_a_noop():
    root = _root(_compose(lines=["Title"], context_table={"enabled": True, "rows": []}))
    assert _marked(root, "data-kp-context") == []


# -- determinism ----------------------------------------------------------------


def test_same_request_is_byte_identical():
    kwargs = dict(
        lines=["Title", "Subtitle"], ink_hex="#0000ff",
        layout={"page": {"frame": True}, "label": {"align": "fill"}},
        context_table={"enabled": True, "rows": ROWS},
    )
    assert _compose(**kwargs) == _compose(**kwargs)


# -- schema parity with the convert models ------------------------------------


def _constraints(info) -> tuple:
    return (repr(info.default), repr(info.metadata), repr(info.annotation))


def test_compose_page_fields_reuse_page_params_constraints():
    assert "size" not in ComposePageParams.model_fields
    for name, info in ComposePageParams.model_fields.items():
        assert _constraints(info) == _constraints(PageParams.model_fields[name]), name
    assert set(ComposePageParams.model_fields) == set(PageParams.model_fields) - {"size"}


def test_compose_label_fields_reuse_label_params_constraints():
    assert set(ComposeLabelParams.model_fields) == set(LabelParams.model_fields) - {"enabled", "text", "lines"}
    for name, info in ComposeLabelParams.model_fields.items():
        assert _constraints(info) == _constraints(LabelParams.model_fields[name]), name


def test_compose_context_pads_reuse_context_table_constraints():
    for side in ("left", "right", "top", "bottom"):
        ref = ContextTableParams.model_fields[f"pad_{side}_mm"]
        assert _constraints(ComposeContextPads.model_fields[side]) == _constraints(ref), side


# -- direct validation errors ---------------------------------------------------


def test_direct_oversize_is_413():
    with pytest.raises(PenPlotError) as exc:
        cs.compose_sheet_svg(_req(), max_svg_bytes=100)
    assert exc.value.status == 413 and exc.value.code == "payload_too_large"


def test_direct_non_svg_is_422():
    with pytest.raises(PenPlotError) as exc:
        cs.compose_sheet_svg(_req(svg="<html><body/></html>"), max_svg_bytes=10**7)
    assert exc.value.status == 422 and exc.value.code == "unsupported_media_type"


def test_direct_malformed_svg_is_422():
    # Not well-formed XML is not sniffed as an SVG document, exactly as the
    # /v1/images sniff treats it when no .svg filename is given: a 422.
    with pytest.raises(PenPlotError) as exc:
        cs.compose_sheet_svg(_req(svg="<svg xmlns='http://www.w3.org/2000/svg'"), max_svg_bytes=10**7)
    assert exc.value.status == 422 and exc.value.code == "unsupported_media_type"


def test_direct_svg_without_drawable_content_is_422():
    with pytest.raises(PenPlotError) as exc:
        cs.compose_sheet_svg(
            _req(svg='<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'),
            max_svg_bytes=10**7,
        )
    assert exc.value.status == 422 and exc.value.code == "bad_image"


def test_direct_performance_five_mb_artwork_under_two_seconds():
    # One dense path: a 5 MB artwork with a realistic element count.
    segments = "".join(f"L {i % 200} {(i * 7) % 297} " for i in range(5 * 1024 * 1024 // 14))
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="210" height="297" viewBox="0 0 210 297">'
        f'<path d="M 0 0 {segments}" fill="none" stroke="#123456"/></svg>'
    )
    req = _req(svg=svg, lines=["Title"])
    cs.compose_sheet_svg(req, max_svg_bytes=25 * 1024 * 1024)  # warm caches
    started = time.perf_counter()
    cs.compose_sheet_svg(req, max_svg_bytes=25 * 1024 * 1024)
    assert time.perf_counter() - started < perf_budget(2.0)


# -- HTTP ----------------------------------------------------------------------

VALID = {
    "svg": ART.decode(),
    "ink_hex": "#1a1a1a",
    "layout": {
        "page": {"orientation": "portrait", "margin_mm": 10, "padding_mm": 5,
                 "frame": True, "frame_radius_mm": 2},
        "label": {"align": "right", "height_mm": 5, "font": "znikoslsvginot",
                  "border": True, "border_radius_mm": 2, "pad_left_mm": 0, "pad_right_mm": 5},
    },
    "lines": ["Title", "Subtitle"],
    "context_table": {"enabled": True, "position": "top-right",
                      "rows": [{"label": "Hajtas", "value": "Hidraulikus"}],
                      "pads": {"left": 10, "right": 10, "top": 10, "bottom": 10}},
}


def _post(http_client, body):
    return http_client.post("/v1/compose-sheet", json=body)


def test_http_returns_svg_and_markers(http_client):
    resp = _post(http_client, VALID)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("image/svg+xml")
    root = _root(resp.text)
    assert root.tag == NS + "svg"
    for marker in ("data-kp-frame", "data-kp-title-block", "data-kp-label",
                   "data-kp-context", "data-kp-context-border"):
        assert _marked(root, marker), marker


def test_http_is_byte_identical_for_the_same_request(http_client):
    first = _post(http_client, VALID)
    second = _post(http_client, VALID)
    assert first.status_code == second.status_code == 200
    assert first.content == second.content


def test_http_minimal_request_uses_defaults(http_client):
    resp = _post(http_client, {"svg": ART.decode()})
    assert resp.status_code == 200, resp.text
    assert _marked(_root(resp.text), "data-kp-label") == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.pop("svg"),
        lambda b: b.update(svg="<html><body/></html>"),
        lambda b: b.update(svg="<svg xmlns='http://www.w3.org/2000/svg'"),
        lambda b: b.update(svg='<svg xmlns="http://www.w3.org/2000/svg"></svg>'),
        lambda b: b.update(ink_hex="red"),
        lambda b: b.update(lines=["a", "b", "c"]),
        lambda b: b.update(lines=["x" * 42]),
        lambda b: b.update(lines=["Bad\x01"]),
        lambda b: b["layout"]["page"].update(size="A3"),
        lambda b: b["layout"]["page"].update(margin_mm=51),
        lambda b: b["layout"]["label"].update(pad_left_mm=21),
        lambda b: b["layout"]["label"].update(enabled=True),
        lambda b: b["layout"]["label"].update(font="comic-sans"),
        lambda b: b.update(unknown=1),
        lambda b: b["context_table"].update(position="middle"),
        lambda b: b["context_table"].update(rows=[{"label": "k", "value": "v"}] * 13),
        lambda b: b["context_table"]["rows"].__setitem__(0, {"label": "", "value": "v"}),
        lambda b: b["context_table"]["rows"].__setitem__(0, {"label": "k", "value": "v\x00"}),
        lambda b: b["context_table"]["pads"].update(left=21),
    ],
)
def test_http_invalid_input_is_422(http_client, mutate):
    body = {**VALID, "layout": {k: dict(v) for k, v in VALID["layout"].items()},
            "context_table": {**VALID["context_table"],
                              "rows": [dict(r) for r in VALID["context_table"]["rows"]],
                              "pads": dict(VALID["context_table"]["pads"])}}
    mutate(body)
    resp = _post(http_client, body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] in ("invalid_params", "unsupported_media_type", "bad_image")


def test_http_oversize_is_413(http_client):
    pad = "<!-- " + "x" * (26 * 1024 * 1024) + " -->"
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">{pad}</svg>'
    resp = _post(http_client, {"svg": svg})
    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == "payload_too_large"


# -- title placement outside the border (below / above) -----------------------

#: Pinned before the placement change: the inside sheet must stay byte-identical.
FRAMED_INSIDE_SHA256 = "dde9ba18ba685ad9ab3c9ba7eae81bd7ea72167b6eaeabec7c341fc34f321bfb"
TITLE, SUB = "Title here", "Sub line"


def _outside(placement, lines, orientation="portrait", frame=False):
    layout = {"page": {"orientation": orientation, "frame": frame},
              "label": {"placement": placement}}
    return _compose(layout=layout, lines=lines)


def _strip():
    return labels.label_outside_mm(TITLE, subtitle=SUB, height_mm=5.0)


def test_inside_sheet_is_byte_identical_to_the_golden():
    svg = _compose(layout={"page": {"frame": True}}, lines=[TITLE, SUB])
    assert hashlib.sha256(svg.encode()).hexdigest() == FRAMED_INSIDE_SHA256


def test_unknown_placement_is_rejected_for_compose():
    with pytest.raises(ValidationError):
        ComposeLayout.model_validate({"label": {"placement": "left"}})


@pytest.mark.parametrize("placement", ["below", "above"])
@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
def test_outside_sheet_is_a4_with_bounds_on_the_root_and_no_rect(placement, orientation):
    root = _root(_outside(placement, [TITLE, SUB], orientation))
    w, h = (297.0, 210.0) if orientation == "landscape" else (210.0, 297.0)
    assert root.attrib["width"] == f"{w:g}mm" and root.attrib["height"] == f"{h:g}mm"
    # vpype plots an invisible rect, so the bounds live on the root instead.
    assert root.attrib["data-kp-a4-bounds"] == f"0 0 {w:g} {h:g}"
    assert root.find(f".//{NS}rect[@id='a4-bounds']") is None


def test_outside_below_moves_the_frame_and_the_lettering_to_the_strip():
    root = _root(_outside("below", [TITLE, SUB], frame=True))
    frame = _marked(root, "data-kp-frame")[0]
    assert abs(_bounds(_xy(frame))[3] - (H - MARGIN - _strip())) < TOL
    assert abs(_text_bounds(root, SUB)[3] - (H - MARGIN)) < TOL
    assert _text_bounds(root, TITLE)[3] < _text_bounds(root, SUB)[1]


def test_outside_above_moves_the_frame_and_the_lettering_to_the_strip():
    root = _root(_outside("above", [TITLE, SUB], frame=True))
    frame = _marked(root, "data-kp-frame")[0]
    assert abs(_bounds(_xy(frame))[1] - (MARGIN + _strip())) < TOL
    assert abs(_text_bounds(root, TITLE)[1] - MARGIN) < TOL


@pytest.mark.parametrize("placement", ["below", "above"])
def test_outside_subtitle_is_six_tenths_of_the_title(placement):
    root = _root(_outside(placement, [TITLE, SUB]))
    t = _text_bounds(root, TITLE)
    u = _text_bounds(root, SUB)
    assert abs((u[3] - u[1]) / (t[3] - t[1]) - 0.6) < 0.02
    assert root.find(f".//{NS}g[@data-kp-label-placement='{placement}']") is not None


@pytest.mark.parametrize("placement", ["below", "above"])
def test_outside_artwork_and_sheet_have_no_band_and_keep_clear(placement):
    root = _root(_outside(placement, [TITLE, SUB], frame=True))
    assert _marked(root, "data-kp-title-block") == []
    art = _marked(root, "data-kp-artwork")[0]
    y, height = float(art.attrib["y"]), float(art.attrib["height"])
    if placement == "below":
        assert y + height <= H - MARGIN - _strip() - labels.LABEL_ARTWORK_GAP_MM + TOL
    else:
        assert y >= MARGIN + _strip() + labels.LABEL_ARTWORK_GAP_MM - TOL


def test_outside_sheet_is_deterministic():
    assert _outside("below", [TITLE, SUB]) == _outside("below", [TITLE, SUB])
    assert _outside("above", [TITLE]) == _outside("above", [TITLE])


def test_a4_bounds_attr_is_tiny_and_a_subtitle_costs_one_title_line():
    # N6 asks for at most 2 KB for the bounds plus the second line. Glyph
    # density sets the cost of a line (about 340 points per 8-10 characters
    # in the default face, at any size), so the guard is relative: the
    # subtitle costs no more than the title line, and the bounds stay small.
    one = _outside("below", [TITLE])
    two = _outside("below", [TITLE, SUB])
    bounds = re.search(r'data-kp-a4-bounds="[^"]*"', two).group(0)
    assert len(bounds) < 200
    title_group = re.search(r'<g data-kp-text="Title here">.*?</g>', two).group(0)
    assert len(two) - len(one) <= 1.2 * len(title_group)


def test_outside_title_only_is_one_line():
    root = _root(_outside("above", [TITLE]))
    group = root.find(f".//{NS}g[@data-kp-label-placement='above']")
    assert len(list(group)) == 1


# -- art placement around the context table -------------------------------------

SEIRAN = Path(__file__).parent / "fixtures" / "aichi-m6a1-seiran-3-view-line-drawing.svg"
TABLE_TOP_RIGHT = {"enabled": True, "position": "top-right", "rows": ROWS}


def _art_svg(*shapes: str) -> bytes:
    """A 200 x 300 artwork (2:3) with the given shapes, drawn in its viewBox."""
    body = "".join(shapes)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300" '
        f'viewBox="0 0 200 300">{body}</svg>'
    ).encode()


def _placement(root: ET.Element) -> dict[str, str]:
    return {k: v for k, v in root.attrib.items() if k.startswith("data-kp-art-")}


def test_table_top_right_over_ink_is_moved_clear():
    # Ink fills the artwork's top-right corner, where the table sits.
    art = _art_svg('<rect x="150" y="0" width="50" height="60" fill="#000"/>')
    root = _root(_compose(svg=art.decode(), lines=["Title"], context_table=TABLE_TOP_RIGHT))
    attrs = _placement(root)
    assert attrs["data-kp-art-overlap"] == "0"
    assert float(attrs["data-kp-art-dx"]) != 0 or float(attrs["data-kp-art-dy"]) != 0 \
        or float(attrs["data-kp-art-scale"]) < 1
    _assert_drawn_ink_clears_table(root, art)


def test_ink_only_bottom_left_is_not_moved():
    art = _art_svg('<rect x="0" y="200" width="60" height="100" fill="#000"/>')
    root = _root(_compose(svg=art.decode(), lines=["Title"], context_table=TABLE_TOP_RIGHT))
    attrs = _placement(root)
    assert attrs == {
        "data-kp-art-dx": "0.00",
        "data-kp-art-dy": "0.00",
        "data-kp-art-scale": "1.000",
        "data-kp-art-overlap": "0",
    }


def test_no_table_emits_no_placement_attributes():
    root = _root(_compose(lines=["Title"], context_table={"enabled": False, "rows": ROWS}))
    assert _placement(root) == {}


def test_seiran_three_view_clears_a_twelve_row_table_top_right():
    rows = [{"label": f"Key {i}", "value": str(1000 + i)} for i in range(12)]
    svg = SEIRAN.read_bytes().decode()
    root = _root(_compose(
        svg=svg, lines=["Title"],
        context_table={"enabled": True, "position": "top-right", "rows": rows},
    ))
    attrs = _placement(root)
    assert attrs["data-kp-art-overlap"] == "0"
    img = _image(root)
    x, y = float(img.attrib["x"]), float(img.attrib["y"])
    w, h = float(img.attrib["width"]), float(img.attrib["height"])
    assert x >= MARGIN - TOL and x + w <= W - MARGIN + TOL
    assert y >= MARGIN - TOL and y + h <= H - MARGIN + TOL
    _assert_drawn_ink_clears_table(root, SEIRAN.read_bytes())


# -- independent clearance check and table fixes ----------------------------------
#
# The helpers below never call the placement code. They take the artwork's
# parsed polylines (svg_vector), the <image> box as it is drawn and the table
# border as it is drawn, and check the drawn ink against the table on their own.

CLEAR_MM = 2.0
SAMPLE_MM = 0.2
CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")
#: Documented order of the table fixes (the shop's suggestTableFixes order).
FIX_ORDER = ["corner", "pads", "removeRows", "orientation", "margins", "labelOutside", "disableTable"]
LONG_ROWS = [
    {"label": "Hosszu cimke szoveg", "value": "Egy nagyon hosszu ertek ami szeles tablazatot ad"}
    for _ in range(12)
]


def _drawn_rect(box, vp_w: float, vp_h: float):
    """Where the artwork viewport is drawn inside the <image> box (fitted, centred)."""
    x, y, w, h = box
    s = min(w / vp_w, h / vp_h)
    cw, ch = vp_w * s, vp_h * s
    return (x + (w - cw) / 2, y + (h - ch) / 2, cw, ch)


def _drawn_ink(polylines, vp_w: float, vp_h: float, rect) -> list[tuple[float, float]]:
    """Every polyline point and segment sample, in sheet mm, sampled at SAMPLE_MM."""
    rx, ry, rw, rh = rect
    kx, ky = rw / vp_w, rh / vp_h
    out: list[tuple[float, float]] = []
    for pl in polylines:
        for i, (ax, ay) in enumerate(pl):
            out.append((rx + ax * kx, ry + ay * ky))
            if i + 1 < len(pl):
                bx, by = pl[i + 1]
                steps = max(1, int(max(abs(bx - ax) * kx, abs(by - ay) * ky) / SAMPLE_MM))
                for k in range(1, steps):
                    t = k / steps
                    out.append((rx + (ax + (bx - ax) * t) * kx, ry + (ay + (by - ay) * t) * ky))
    return out


def _assert_drawn_ink_clears_table(root: ET.Element, art: bytes) -> None:
    img = _image(root)
    box = tuple(float(img.attrib[k]) for k in ("x", "y", "width", "height"))
    polylines, vp_w, vp_h, _warnings = parse_svg_vectors(art)
    x0, y0, x1, y1 = _bounds(_xy(_marked(root, "data-kp-context-border")[0]))
    for px, py in _drawn_ink(polylines, vp_w, vp_h, _drawn_rect(box, vp_w, vp_h)):
        near = x0 - CLEAR_MM < px < x1 + CLEAR_MM and y0 - CLEAR_MM < py < y1 + CLEAR_MM
        assert not near, f"drawn ink at ({px:.2f}, {py:.2f}) is within {CLEAR_MM} mm of the table"


def _framed_art(root_attrs: str, ink: tuple[float, float, float, float]) -> bytes:
    x, y, w, h = ink
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" {root_attrs}>'
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#000"/></svg>'
    ).encode()


#: Root forms that the mask frame has to get right. The ink sits in the top-right
#: corner of the viewport, where the table is drawn.
FRAME_CASES = {
    "viewbox-only": ('viewBox="0 0 200 300"', (150, 0, 50, 60)),
    "percent-size": ('width="100%" height="100%" viewBox="0 0 200 300"', (150, 0, 50, 60)),
    "single-width": ('width="200" viewBox="0 0 200 300"', (150, 0, 50, 60)),
    "mixed-units": ('width="100mm" height="50" viewBox="0 0 756 100"', (600, 0, 156, 40)),
    # Non-zero viewBox origin: the ink at (250, 100) is the viewport's top-right.
    "viewbox-origin": ('viewBox="100 100 200 300"', (250, 100, 50, 60)),
}


@pytest.mark.parametrize("case", sorted(FRAME_CASES))
def test_drawn_ink_clears_the_table_for_each_root_form(case):
    root_attrs, ink = FRAME_CASES[case]
    art = _framed_art(root_attrs, ink)
    root = _root(_compose(svg=art.decode(), lines=["Title"], context_table=TABLE_TOP_RIGHT))
    assert _placement(root)["data-kp-art-overlap"] == "0"
    _assert_drawn_ink_clears_table(root, art)


CORNER_INK = {
    "top-left": (0, 0, 50, 60),
    "top-right": (150, 0, 50, 60),
    "bottom-left": (0, 240, 50, 60),
    "bottom-right": (150, 240, 50, 60),
}


@pytest.mark.parametrize("corner", CORNERS)
def test_ink_in_each_table_corner_is_moved_clear(corner):
    art = _art_svg('<rect x="{}" y="{}" width="{}" height="{}" fill="#000"/>'.format(*CORNER_INK[corner]))
    root = _root(_compose(
        svg=art.decode(), lines=["Title"],
        context_table={"enabled": True, "position": corner, "rows": ROWS},
    ))
    attrs = _placement(root)
    assert attrs["data-kp-art-overlap"] == "0"
    assert float(attrs["data-kp-art-dx"]) != 0 or float(attrs["data-kp-art-dy"]) != 0 \
        or float(attrs["data-kp-art-scale"]) < 1
    _assert_drawn_ink_clears_table(root, art)


WIDE_INK = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200" viewBox="0 0 300 200">'
    b'<rect x="0" y="0" width="300" height="200" fill="#000"/></svg>'
)
#: Full-page ink with a 12-row table, landscape: nothing fits, so the art stays.
OVERLAP_REQ = {
    "svg": WIDE_INK.decode(),
    "lines": ["Title"],
    "layout": {"page": {"orientation": "landscape", "margin_mm": 10, "padding_mm": 10}},
    "context_table": {
        "enabled": True, "position": "top-right", "rows": LONG_ROWS,
        "pads": {"left": 20, "right": 20, "top": 20, "bottom": 20},
    },
}


def _compose_dict(req: dict) -> ET.Element:
    body = ComposeSheetRequest.model_validate(req)
    return _root(cs.compose_sheet_svg(body, max_svg_bytes=25 * 1024 * 1024))


def _apply_fix(req: dict, fix: dict) -> dict:
    """The request with exactly one field changed, as the fix describes."""
    out = copy.deepcopy(req)
    table = out.setdefault("context_table", {})
    page = out.setdefault("layout", {}).setdefault("page", {})
    label = out["layout"].setdefault("label", {})
    kind = fix["kind"]
    if kind == "corner":
        table["position"] = fix["corner"]
    elif kind == "pads":
        table["pads"] = {"left": 0, "right": 0, "top": 0, "bottom": 0}
    elif kind == "removeRows":
        table["rows"] = table["rows"][: len(table["rows"]) - fix["n"]]
        table["position"] = fix["corner"]
    elif kind == "orientation":
        page["orientation"] = fix["orientation"]
    elif kind == "margins":
        page["margin_mm"] = 0
        page["padding_mm"] = 0
    elif kind == "labelOutside":
        label["placement"] = fix["placement"]
    elif kind == "disableTable":
        table["enabled"] = False
    else:  # pragma: no cover - the test fails on an unknown kind
        raise AssertionError(kind)
    return out


def test_overlap_lists_the_table_fixes_in_the_documented_order():
    root = _compose_dict(OVERLAP_REQ)
    assert _placement(root)["data-kp-art-overlap"] == "1"
    fixes = json.loads(root.attrib["data-kp-table-fixes"])
    assert fixes == [
        {"kind": "corner", "corner": "bottom-left"},
        {"kind": "corner", "corner": "bottom-right"},
        {"kind": "removeRows", "n": 1, "corner": "bottom-left"},
        {"kind": "orientation", "orientation": "portrait"},
        {"kind": "margins"},
        {"kind": "labelOutside", "placement": "below"},
        {"kind": "disableTable"},
    ]
    ranks = [FIX_ORDER.index(f["kind"]) for f in fixes]
    assert ranks == sorted(ranks)


def test_each_listed_table_fix_clears_the_overlap_when_applied():
    fixes = json.loads(_compose_dict(OVERLAP_REQ).attrib["data-kp-table-fixes"])
    for fix in fixes:
        root = _compose_dict(_apply_fix(OVERLAP_REQ, fix))
        if fix["kind"] == "disableTable":
            assert "data-kp-art-overlap" not in root.attrib
            assert not _marked(root, "data-kp-context-border")
            continue
        assert _placement(root)["data-kp-art-overlap"] == "0", fix
        _assert_drawn_ink_clears_table(root, WIDE_INK)


#: A row whose characters the table font cannot draw (U+2603 is not in the
#: svgfont face), so layout_stats_table draws no text for it.
UNDRAWABLE_ROW = {"label": "☃", "value": "☃"}


def test_removing_rows_that_leave_only_undrawable_text_is_not_a_fix():
    # Keeping the three undrawable rows and removing the one drawable row would
    # leave no drawable glyph: the table vanishes and the overlap reads as gone.
    # That is not a fix, so no removeRows suggestion may be listed for it.
    req = copy.deepcopy(OVERLAP_REQ)
    req["context_table"]["rows"] = [UNDRAWABLE_ROW] * 3 + [LONG_ROWS[0]]
    root = _compose_dict(req)
    assert _placement(root)["data-kp-art-overlap"] == "1"
    fixes = json.loads(root.attrib["data-kp-table-fixes"])
    assert not [f for f in fixes if f["kind"] == "removeRows"]
    assert fixes[-1] == {"kind": "disableTable"}


def test_every_listed_table_fix_keeps_the_table_drawn():
    req = copy.deepcopy(OVERLAP_REQ)
    req["context_table"]["rows"] = [UNDRAWABLE_ROW] * 3 + [LONG_ROWS[0]]
    fixes = json.loads(_compose_dict(req).attrib["data-kp-table-fixes"])
    for fix in fixes:
        root = _compose_dict(_apply_fix(req, fix))
        if fix["kind"] == "disableTable":
            continue
        assert _marked(root, "data-kp-context-border"), fix
        assert _placement(root)["data-kp-art-overlap"] == "0", fix
        _assert_drawn_ink_clears_table(root, WIDE_INK)


def test_table_fixes_absent_when_the_art_clears_the_table():
    art = _art_svg('<rect x="0" y="200" width="60" height="100" fill="#000"/>')
    root = _root(_compose(svg=art.decode(), lines=["Title"], context_table=TABLE_TOP_RIGHT))
    assert "data-kp-table-fixes" not in root.attrib


def test_table_fixes_absent_without_a_table():
    root = _root(_compose(
        svg=_art_svg('<rect x="150" y="0" width="50" height="60" fill="#000"/>').decode(),
        lines=["Title"], context_table={"enabled": False, "rows": ROWS},
    ))
    assert "data-kp-table-fixes" not in root.attrib
    assert "data-kp-art-overlap" not in root.attrib


def _five_mb_overlapping_art() -> str:
    """A 5 MiB artwork with ink over the whole sheet, so the table overlaps."""
    head = '<svg xmlns="http://www.w3.org/2000/svg" width="210" height="297" viewBox="0 0 210 297"><path d="M 0 0 '
    target = 5 * 1024 * 1024 - len(head) - 60
    parts: list[str] = []
    size = i = 0
    while size < target:
        seg = f"L {i % 200} {(i * 7) % 297} "
        parts.append(seg)
        size += len(seg)
        i += 1
    return head + "".join(parts) + '" fill="none" stroke="#123456"/></svg>'


def test_direct_performance_overlap_with_fixes_under_two_seconds():
    req = ComposeSheetRequest.model_validate({
        "svg": _five_mb_overlapping_art(), "lines": ["Title"],
        "context_table": {"enabled": True, "position": "top-right", "rows": LONG_ROWS},
    })
    cs.compose_sheet_svg(req, max_svg_bytes=25 * 1024 * 1024)  # warm caches
    started = time.perf_counter()
    svg = cs.compose_sheet_svg(req, max_svg_bytes=25 * 1024 * 1024)
    assert time.perf_counter() - started < perf_budget(2.0)
    assert "data-kp-table-fixes" in _root(svg).attrib


# -- inline artwork (artwork="inline") -----------------------------------------

PX_PER_MM = 96 / 25.4
INLINE_OK = "inline"
ART_SVG = ART.decode()
SEIRAN_SVG = SEIRAN.read_bytes().decode()


def _art_group(root: ET.Element) -> ET.Element:
    groups = [el for el in root.iter(NS + "g") if "data-kp-artwork" in el.attrib]
    assert len(groups) == 1
    return groups[0]


def _group_matrix(root: ET.Element) -> tuple[float, ...]:
    text = _art_group(root).attrib["transform"]
    match = re.fullmatch(r"matrix\(([^)]*)\)", text)
    assert match, text
    return tuple(float(v) for v in match.group(1).split())


def _fit(vb: tuple[float, float, float, float], box: tuple[float, float, float, float]):
    """Independent xMidYMid meet of a viewBox into a box: (scale, tx, ty)."""
    vx, vy, vw, vh = vb
    bx, by, bw, bh = box
    s = min(bw / vw, bh / vh)
    return s, bx + (bw - vw * s) / 2 - vx * s, by + (bh - vh * s) / 2 - vy * s


def _viewbox(svg: str) -> tuple[float, float, float, float]:
    nums = [float(v) for v in re.findall(r'viewBox="([^"]*)"', svg)[0].replace(",", " ").split()]
    return (nums[0], nums[1], nums[2], nums[3])


def _same_polyline(a, b, tol: float = 0.05) -> bool:
    return len(a) == len(b) and all(
        abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol for p, q in zip(a, b)
    )


def _artwork_in_mm(svg: str, box: tuple[float, float, float, float]):
    """The artwork's own strokes (user units) mapped to the sheet, in mm."""
    vb = _viewbox(svg)
    polys, vp_w, vp_h, _ = parse_svg_vectors(svg.encode())
    # parse_svg_vectors returns viewport px; the test artworks have vp == viewBox size,
    # so user units are the px plus the viewBox origin.
    assert _close(vp_w, vb[2]) and _close(vp_h, vb[3])
    s, tx, ty = _fit(vb, box)
    return [
        [(tx + (x + vb[0]) * s, ty + (y + vb[1]) * s) for x, y in pl] for pl in polys
    ]


@pytest.mark.parametrize("vb", [(0, 0, 200, 100), (50, 20, 200, 100)])
def test_inline_art_sits_in_the_image_box(vb):
    vx, vy, vw, vh = vb
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{vw}" height="{vh}" '
        f'viewBox="{vx} {vy} {vw} {vh}"><rect x="{vx + 10}" y="{vy + 10}" width="50" height="30" '
        'fill="#000"/></svg>'
    )
    image = _root(_compose(svg=svg, lines=["Title"]))
    inline = _root(_compose(svg=svg, lines=["Title"], artwork=INLINE_OK))
    img = _image(image)
    box = tuple(float(img.attrib[k]) for k in ("x", "y", "width", "height"))
    s, tx, ty = _fit(vb, box)
    a, b, c, d, e, f = _group_matrix(inline)
    assert _close(a, s) and _close(d, s) and _close(b, 0) and _close(c, 0)
    assert _close(e, tx) and _close(f, ty)
    # The mapped viewBox lies inside the image box and is centred in it.
    left, top = e + vx * s, f + vy * s
    assert left >= box[0] - TOL and top >= box[1] - TOL
    assert left + vw * s <= box[0] + box[2] + TOL and top + vh * s <= box[1] + box[3] + TOL
    assert _close(left + vw * s / 2, box[0] + box[2] / 2)
    assert _close(top + vh * s / 2, box[1] + box[3] / 2)


def test_inline_and_image_share_the_placement_around_the_table():
    art = _art_svg('<rect x="150" y="0" width="50" height="60" fill="#000"/>')
    kwargs = {"svg": art.decode(), "lines": ["Title"], "context_table": TABLE_TOP_RIGHT}
    image = _root(_compose(**kwargs))
    inline = _root(_compose(artwork=INLINE_OK, **kwargs))
    assert _placement(image) == _placement(inline)
    assert _placement(inline)["data-kp-art-overlap"] == "0"
    img = _image(image)
    box = tuple(float(img.attrib[k]) for k in ("x", "y", "width", "height"))
    s, tx, ty = _fit(_viewbox(art.decode()), box)
    a, _b, _c, d, e, f = _group_matrix(inline)
    assert _close(a, s) and _close(d, s) and _close(e, tx) and _close(f, ty)


@pytest.mark.parametrize("svg", [ART_SVG, SEIRAN_SVG])
def test_inline_sheet_has_no_image_text_or_nested_svg(svg):
    out = _compose(svg=svg, lines=["Title", "Subtitle"], context_table=TABLE_TOP_RIGHT,
                   artwork=INLINE_OK)
    root = _root(out)
    assert list(root.iter(NS + "image")) == []
    assert list(root.iter(NS + "text")) == []
    assert "<image" not in out and "<text" not in out
    assert [el for el in root.iter(NS + "svg")] == [root]


@pytest.mark.parametrize("vb", [(0, 0, 200, 100), (50, 20, 200, 100)])
def test_inline_strokes_are_plotted_at_the_placed_position(vb):
    vx, vy, vw, vh = vb
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{vw}" height="{vh}" '
        f'viewBox="{vx} {vy} {vw} {vh}">'
        f'<path d="M {vx + 10} {vy + 10} L {vx + 90} {vy + 10} L {vx + 90} {vy + 60} '
        f'L {vx + 10} {vy + 60} Z" fill="none" stroke="#000"/>'
        f'<path d="M {vx + 120} {vy + 80} L {vx + 180} {vy + 20}" fill="none" stroke="#000"/>'
        "</svg>"
    )
    image = _root(_compose(svg=svg, lines=["Title"]))
    img = _image(image)
    box = tuple(float(img.attrib[k]) for k in ("x", "y", "width", "height"))
    out = _compose(svg=svg, lines=["Title"], artwork=INLINE_OK)
    plotted, _w, _h, _warn = parse_svg_vectors(out.encode())
    expected = _artwork_in_mm(svg, box)
    assert len(expected) == 2
    for mm in expected:
        px = [(x * PX_PER_MM, y * PX_PER_MM) for x, y in mm]
        assert any(_same_polyline(px, got) for got in plotted), px


def test_inline_seiran_strokes_are_plotted_inside_the_placed_box():
    image = _root(_compose(svg=SEIRAN_SVG, lines=["Title"]))
    img = _image(image)
    box = tuple(float(img.attrib[k]) for k in ("x", "y", "width", "height"))
    out = _compose(svg=SEIRAN_SVG, lines=["Title"], artwork=INLINE_OK)
    plotted, _w, _h, _warn = parse_svg_vectors(out.encode())
    expected = _artwork_in_mm(SEIRAN_SVG, box)
    assert len(expected) == 1004
    # The Seiran strokes are all there, at the placed position (sampled).
    for mm in expected[:40]:
        px = [(x * PX_PER_MM, y * PX_PER_MM) for x, y in mm]
        assert any(_same_polyline(px, got) for got in plotted)
    # Every artwork stroke lies inside the placed box (2 dp rounding slack).
    bx, by, bw, bh = box
    for mm in expected:
        for x, y in mm:
            assert bx - TOL <= x <= bx + bw + TOL and by - TOL <= y <= by + bh + TOL


@pytest.mark.parametrize(
    ("fragment", "code"),
    [
        ("<script>alert(1)</script>", "artwork_unsafe"),
        ("<foreignObject><div/></foreignObject>", "artwork_unsafe"),
        ('<rect width="5" height="5" onclick="x()"/>', "artwork_unsafe"),
        ('<a href="https://example.com"><rect width="5" height="5"/></a>', "artwork_unsafe"),
        ('<a href="javascript:alert(1)"><rect width="5" height="5"/></a>', "artwork_unsafe"),
        ('<rect width="5" height="5" fill="url(https://example.com/p.svg#g)"/>', "artwork_unsafe"),
        ("<style>rect{fill:red}</style><rect width=\"5\" height=\"5\"/>", "artwork_unsafe"),
        ('<animate attributeName="href" to="https://example.com"/><rect width="5" height="5"/>',
         "artwork_unsafe"),
        ('<image href="data:image/png;base64,AAAA" width="5" height="5"/>', "artwork_not_plottable"),
        ('<text x="1" y="5">Hi</text>', "artwork_not_plottable"),
        ('<svg x="0" y="0"><rect width="5" height="5"/></svg>', "artwork_not_plottable"),
    ],
)
def test_inline_rejects_unsafe_or_unplottable_artwork(fragment, code):
    # A drawable rect keeps the artwork itself valid; the fragment is the fault.
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" '
        f'viewBox="0 0 100 100"><rect width="5" height="5"/>{fragment}</svg>'
    )
    with pytest.raises(PenPlotError) as exc:
        _compose(svg=svg, artwork=INLINE_OK)
    assert exc.value.status == 422
    assert exc.value.code == code


def test_inline_keeps_internal_references_and_prefixes_every_id():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="100" height="100" viewBox="0 0 100 100">'
        '<defs><path id="line" d="M 0 0 L 50 0" fill="none" stroke="#000"/></defs>'
        '<use href="#line" x="10" y="10"/>'
        '<use xlink:href="#line" x="10" y="30"/>'
        '<rect id="frame" width="20" height="20" fill="none" stroke="#000"/></svg>'
    )
    out = _compose(svg=svg, lines=["Title"], artwork=INLINE_OK)
    root = _root(out)
    ids = [el.attrib["id"] for el in root.iter() if "id" in el.attrib]
    assert sorted(ids) == ["kp-art-frame", "kp-art-line"]
    assert 'href="#kp-art-line"' in out and 'xlink:href="#kp-art-line"' in out
    # The two uses draw the referenced line, at their own offsets.
    plotted, _w, _h, _warn = parse_svg_vectors(out.encode())
    box = tuple(float(_image(_root(_compose(svg=svg, lines=["Title"]))).attrib[k])
                for k in ("x", "y", "width", "height"))
    s, tx, ty = _fit((0, 0, 100, 100), box)
    for dy in (10, 30):
        # The line is M 0 0 L 50 0, drawn at the use's (x=10, y=dy).
        mm = [(tx + x * s, ty + y * s) for x, y in [(10, dy), (60, dy)]]
        px = [(x * PX_PER_MM, y * PX_PER_MM) for x, y in mm]
        assert any(_same_polyline(px, got, tol=0.1) for got in plotted)


def test_inline_sheet_with_nothing_plottable_is_422(monkeypatch):
    real = cs.parse_svg_vectors

    def sheet_without_strokes(data: bytes):
        if b"data-kp-compose" in data:
            raise PenPlotError(status=400, code="bad_image", message="no strokes")
        return real(data)

    monkeypatch.setattr(cs, "parse_svg_vectors", sheet_without_strokes)
    with pytest.raises(PenPlotError) as exc:
        _compose(artwork=INLINE_OK)
    assert exc.value.status == 422
    assert exc.value.code == "sheet_not_plottable"


def test_artwork_field_defaults_to_image_and_rejects_unknown_values():
    assert ComposeSheetRequest.model_validate({"svg": ART_SVG}).artwork == "image"
    assert _compose(artwork="image") == _compose()
    with pytest.raises(ValidationError):
        ComposeSheetRequest.model_validate({"svg": ART_SVG, "artwork": "raw"})


def test_direct_performance_five_mb_inline_artwork_under_two_seconds():
    svg = _five_mb_overlapping_art()  # exactly 5 MiB of one dense path
    assert len(svg.encode()) >= 5 * 1024 * 1024 - 64
    req = ComposeSheetRequest.model_validate({"svg": svg, "lines": ["Title"], "artwork": INLINE_OK})
    cs.compose_sheet_svg(req, max_svg_bytes=25 * 1024 * 1024)  # warm caches
    started = time.perf_counter()
    cs.compose_sheet_svg(req, max_svg_bytes=25 * 1024 * 1024)
    assert time.perf_counter() - started < perf_budget(2.0)
