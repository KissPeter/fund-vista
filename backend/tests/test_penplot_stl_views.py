"""Pure-algorithm tests for STL -> side-view plotter artwork.

No server is needed: :mod:`backend.penplot.stl_views` is geometry only, and
``compose_sheet_svg`` is a pure function. Real HTTP coverage for
``POST /v1/compose-sheet`` lives in ``test_penplot_compose_sheet.py``.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("slice3d")
pytest.importorskip("trimesh")

from backend.penplot import compose_sheet as cs
from backend.penplot import stl2plot, stl_views
from backend.penplot.schemas import ComposeSheetRequest
from backend.penplot.svg_vector import parse_svg_vectors


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


def test_side_view_uses_second_axis_as_depth(tmp_path):
    path = tmp_path / "box.stl"
    tris = _cube()
    tris[:, :, 1] *= 4.0
    tris[:, :, 2] *= 2.0
    _binary_stl(path, tris)
    mesh = stl_views.load_stl(path)
    assert stl_views.canonical_axes(mesh) == (0, 2, 1)
    svg = stl_views.render_side_view(mesh, slices=8)
    lines, w, h, warnings = parse_svg_vectors(svg.encode())
    assert not warnings and lines
    assert w > h  # longest axis horizontal, thinnest axis vertical


def test_invalid_options_are_rejected():
    with pytest.raises(ValueError):
        stl_views.render_side_view(_cube(), slices=0)
    with pytest.raises(ValueError):
        stl_views.render_side_view(_cube(), fit_mm=0)
    with pytest.raises(stl_views.StlFormatError):
        stl_views.render_side_view("missing.stl")


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
