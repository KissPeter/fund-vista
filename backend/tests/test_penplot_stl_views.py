"""Pure-algorithm tests for STL -> three plotter artworks.

No server is needed: :mod:`backend.penplot.stl_views` is geometry only, and
``compose_sheet_svg`` is a pure function. Real HTTP coverage for
``POST /v1/compose-sheet`` lives in ``test_penplot_stl_api.py`` and
``test_penplot_compose_sheet.py``.

The external toolchain is NOT a backend dependency (licence-inventory gate).
Geometry tests skip when it is absent; ``test_toolchain_importable`` is the
single explicit opt-in check for hosts that install it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import struct
from pathlib import Path

import numpy as np
import pytest

from backend.penplot import compose_sheet as cs
from backend.penplot import stl2plot, stl_views
from backend.penplot.schemas import ComposeSheetRequest
from backend.penplot.svg_vector import parse_svg_vectors

_TOOLCHAIN = bool(importlib.util.find_spec("slice3d") and importlib.util.find_spec("trimesh"))


@pytest.fixture(autouse=True)
def _needs_toolchain(request):
    """Skip geometry cases on hosts without slice3d/trimesh.

    This replaces the old module-level ``pytest.importorskip``: collection now
    succeeds in ``backend/.venv`` and the one explicit toolchain test decides
    whether absence is a failure (``PENPLOT_STL_REQUIRE_TOOLCHAIN=1``) or a
    documented skip.
    """
    if request.node.name == "test_toolchain_importable":
        return
    if not _TOOLCHAIN:
        pytest.skip("slice3d/trimesh are optional runtime tools, not backend dependencies")


def test_toolchain_importable():
    """Opt-in host check: fails loudly only when the toolchain is required."""
    if os.environ.get("PENPLOT_STL_REQUIRE_TOOLCHAIN") != "1":
        pytest.skip("set PENPLOT_STL_REQUIRE_TOOLCHAIN=1 to require the runtime tools")
    try:
        import slice3d  # noqa: F401
        import trimesh  # noqa: F401
    except ModuleNotFoundError as exc:
        pytest.fail(f"STL toolchain is required but not importable: {exc}")


def _cube() -> np.ndarray:
    v = np.array([
        [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
        [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1],
    ], dtype=np.float64)
    f = np.array([
        [0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
        [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
        [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7],
    ])
    return v[f]


def _box() -> np.ndarray:
    """Extents x=4, y=2, z=1: three distinct axes for the view table."""
    tris = _cube()
    tris[:, :, 0] *= 4.0
    tris[:, :, 1] *= 2.0
    return tris


def _binary_stl(path, tris: np.ndarray) -> None:
    raw = bytearray(b"solid cube") + b"\0" * 70 + struct.pack("<I", len(tris))
    for tri in tris:
        n = np.cross(tri[1] - tri[0], tri[2] - tri[0])
        n /= np.linalg.norm(n)
        raw += struct.pack("<12fH", *n, *tri.ravel(), 0)
    path.write_bytes(raw)


def _ascii_stl(path, tris: np.ndarray) -> None:
    lines = ["solid cube"]
    for tri in tris:
        lines += ["facet normal 0 0 0", "outer loop"]
        lines += [f"vertex {x:.6f} {y:.6f} {z:.6f}" for x, y, z in tri]
        lines += ["endloop", "endfacet"]
    lines.append("endsolid cube")
    path.write_text("\n".join(lines))


# -- parsing classes -----------------------------------------------------------

def test_binary_stl_parses_triangles(tmp_path):
    path = tmp_path / "cube.stl"
    _binary_stl(path, _cube())
    mesh = stl_views.load_stl(path)
    assert mesh.faces.shape == (12, 3)
    assert mesh.vertices.shape == (8, 3)  # trimesh welds identical corners
    np.testing.assert_allclose(mesh.bounds, [[0, 0, 0], [1, 1, 1]])


def test_ascii_stl_parses_triangles(tmp_path):
    path = tmp_path / "cube.stl"
    _ascii_stl(path, _cube())
    mesh = stl_views.load_stl(path)
    assert mesh.faces.shape == (12, 3)


def test_truncated_binary_header_is_rejected(tmp_path):
    path = tmp_path / "bad.stl"
    path.write_bytes(b"solid" + b"\0" * 20)
    with pytest.raises(stl_views.StlFormatError):
        stl_views.load_stl(path)


def test_non_stl_text_is_rejected(tmp_path):
    path = tmp_path / "bad.stl"
    path.write_text("this is not a mesh\n" * 10)
    with pytest.raises(stl_views.StlFormatError):
        stl_views.load_stl(path)


# -- geometry classes ----------------------------------------------------------

def test_facet_order_does_not_change_the_svg(tmp_path):
    tris = _cube()
    binary = tmp_path / "cube.stl"
    ascii_ = tmp_path / "cube-ascii.stl"
    _binary_stl(binary, tris)
    _ascii_stl(ascii_, tris[::-1])
    assert stl_views.render_side_view(binary, slices=4) == stl_views.render_side_view(ascii_, slices=4)


def test_side_view_is_artwork_only(tmp_path):
    path = tmp_path / "cube.stl"
    _binary_stl(path, _cube())
    svg = stl_views.render_side_view(path, slices=4)
    assert "<path" in svg and 'fill="none"' in svg
    assert "<text" not in svg and "<image" not in svg
    assert "mm\"" in svg  # physical units, not a bare pixel canvas

    lines, w, h, warnings = parse_svg_vectors(svg.encode())
    assert not warnings
    assert len(lines) >= 4
    assert w > 0 and h > 0


def test_side_view_is_the_broad_alias(tmp_path):
    path = tmp_path / "box.stl"
    _binary_stl(path, _box())
    assert stl_views.render_side_view(path, slices=4) == stl_views.render_view(
        path, view="broad", slices=4
    )


def test_canonical_axes_break_ties_by_index(tmp_path):
    path = tmp_path / "cube.stl"
    _binary_stl(path, _cube())
    assert stl_views.canonical_axes(stl_views.load_stl(path)) == (0, 1, 2)


@pytest.mark.parametrize(
    ("view", "slice_axis", "x_axis", "y_axis"),
    [("broad", 2, 0, 1), ("edge", 1, 0, 2), ("end", 0, 1, 2)],
)
def test_projection_table(tmp_path, view, slice_axis, x_axis, y_axis):
    path = tmp_path / "box.stl"
    _binary_stl(path, _box())
    mesh = stl_views.load_stl(path)
    assert stl_views.view_axes(view, mesh) == (slice_axis, x_axis, y_axis)
    svg = stl_views.render_view(mesh, view=view, slices=4)
    lines, vp_w, vp_h, warnings = parse_svg_vectors(svg.encode())
    assert not warnings and vp_w > vp_h  # long axis is always horizontal
    # The viewport includes the symmetric margin; the projection ratio is the
    # bounding box of the drawn cross-sections themselves.
    xs = [x for polyline in lines for x, _ in polyline]
    ys = [y for polyline in lines for _, y in polyline]
    drawn_w = max(xs) - min(xs)
    drawn_h = max(ys) - min(ys)
    assert abs(drawn_w / drawn_h - (2.0 if view != "edge" else 4.0)) < 0.02


def test_three_views_are_deterministic_and_artwork_only(tmp_path):
    path = tmp_path / "box.stl"
    _binary_stl(path, _box())
    first = stl_views.render_three_views(path, slices=4)
    second = stl_views.render_three_views(path, slices=4)
    assert tuple(first) == stl_views.VIEW_NAMES == ("broad", "edge", "end")
    assert first == second  # dict equality covers every view's bytes
    for svg in first.values():
        assert "<text" not in svg and "<image" not in svg
        assert 'fill="none"' in svg


@pytest.mark.parametrize(
    ("kwargs", "ok"),
    [
        ({"slices": 1}, False), ({"slices": 2}, True),
        ({"slices": 16}, True), ({"slices": 17}, False),
        ({"fit_mm": 19.0}, False), ({"fit_mm": 20.0}, True),
        ({"fit_mm": 400.0}, True), ({"fit_mm": 401.0}, False),
        ({"margin_mm": -0.1}, False), ({"margin_mm": 0.0}, True),
        ({"margin_mm": 20.0}, True), ({"margin_mm": 20.1}, False),
        ({"stroke_mm": 0.09}, False), ({"stroke_mm": 0.1}, True),
        ({"stroke_mm": 1.0}, True), ({"stroke_mm": 1.01}, False),
    ],
)
def test_option_caps(kwargs, ok):
    opts = {"slices": 8, "fit_mm": 180.0, "margin_mm": 5.0, "stroke_mm": 0.2}
    opts.update(kwargs)
    check = lambda: stl_views._check_options(**opts)
    if ok:
        check()
    else:
        with pytest.raises(ValueError):
            check()


def test_unknown_view_is_rejected(tmp_path):
    path = tmp_path / "box.stl"
    _binary_stl(path, _box())
    with pytest.raises(ValueError, match="unknown view"):
        stl_views.render_view(path, view="side")


# -- gallery hand-off ----------------------------------------------------------

def test_artwork_composes_as_inline_plot_ready_sheet(tmp_path):
    path = tmp_path / "cube.stl"
    _binary_stl(path, _cube())
    svg = stl_views.render_side_view(path, slices=4)
    body = ComposeSheetRequest.model_validate(
        {"svg": svg, "lines": ["Knipex XS"], "artwork": "inline"})
    sheet = cs.compose_sheet_svg(body, max_svg_bytes=25 * 1024 * 1024)
    assert "<image" not in sheet and "<text" not in sheet
    assert "data-kp-artwork" in sheet


def test_real_sample_side_view_is_plot_ready():
    path = Path(__file__).parent / "fixtures" / "knipexXs.stl"
    svg = stl_views.render_side_view(path)
    assert svg.count("<path ") > 10
    lines, _, _, warnings = parse_svg_vectors(svg.encode())
    assert not warnings and len(lines) > 10
    body = ComposeSheetRequest.model_validate({"svg": svg, "artwork": "inline"})
    assert "<image" not in cs.compose_sheet_svg(body, max_svg_bytes=25 * 1024 * 1024)


# -- CLI classes ---------------------------------------------------------------

def test_cli_writes_artwork_and_report(tmp_path):
    src = tmp_path / "part.stl"
    out = tmp_path / "out" / "part.svg"
    report = tmp_path / "report.json"
    _binary_stl(src, _cube())
    assert stl2plot.main([str(src), "-o", str(out), "--report", str(report),
                          "--slices", "4", "-q"]) == 0
    assert "<path" in out.read_text()
    data = json.loads(report.read_text())
    assert data["summary"]["ok"] == 1 and data["summary"]["failed"] == 0
    assert data["results"][0]["strokes"] >= 3


def test_cli_reports_a_bad_stl_as_failure(tmp_path):
    src = tmp_path / "bad.stl"
    src.write_text("not a mesh")
    assert stl2plot.main([str(src), "--slices", "4", "-q"]) == 1


def test_cli_several_inputs_need_out_dir(tmp_path):
    with pytest.raises(SystemExit) as exc:
        stl2plot.main([str(tmp_path / "a.stl"), str(tmp_path / "b.stl")])
    assert exc.value.code == 2
