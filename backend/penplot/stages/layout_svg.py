"""Layout + SVG emission stages (REF-002 Phase 4).

Pure move from :mod:`backend.penplot.optimize` — identical behavior,
with two touch-ups: the 158-char ``layout_scale`` signature is wrapped,
and the ``LINE_COLORS`` import is hoisted to module level.
"""

from __future__ import annotations

import logging

from backend.penplot.backgrounds import LINE_COLORS
from backend.penplot.config import PAGE_SIZES_MM
from backend.penplot.methods import Polyline

log = logging.getLogger(__name__)


def page_dims_mm(size: str, orientation: str, margin_mm: float) -> tuple[float, float]:
    w, h = PAGE_SIZES_MM[size.upper()]
    if orientation == "landscape":
        w, h = h, w
    return w, h


def layout(
    lines_px: list[Polyline],
    src_w: float,
    src_h: float,
    *,
    size: str,
    orientation: str,
    margin_mm: float,
    reserve_bottom_mm: float = 0.0,
    padding_mm: float = 0.0,
) -> tuple[list[Polyline], float, float]:
    """Scale pixel polylines into the margined page rect (mm). Returns (lines, W, H).

    ``reserve_bottom_mm`` keeps a strip above the bottom margin free (the
    title-block label zone) — artwork centers in the remaining area and
    bottoms out exactly where the label divider will sit.
    ``padding_mm`` is internal breathing room inside the margin/frame on all
    sides, so artwork never touches the border (B-001).
    """
    page_w, page_h = page_dims_mm(size, orientation, margin_mm)
    pad = max(float(padding_mm), 0.0)
    draw_w = max(1e-6, page_w - 2 * margin_mm - 2 * pad)
    draw_h = max(1e-6, page_h - 2 * margin_mm - max(reserve_bottom_mm, 0.0) - 2 * pad)
    scale = min(draw_w / max(src_w, 1e-6), draw_h / max(src_h, 1e-6))
    ox = margin_mm + pad + (draw_w - src_w * scale) / 2.0
    oy = margin_mm + pad + (draw_h - src_h * scale) / 2.0
    out = [
        [(ox + x * scale, oy + y * scale) for x, y in pl]
        for pl in lines_px
    ]
    return out, page_w, page_h


def layout_scale(
    src_w: float, src_h: float, size: str, orientation: str, margin_mm: float,
    reserve_bottom_mm: float = 0.0, padding_mm: float = 0.0,
) -> float:
    page_w, page_h = page_dims_mm(size, orientation, margin_mm)
    pad = max(float(padding_mm), 0.0)
    return min(
        max(1e-6, page_w - 2 * margin_mm - 2 * pad) / max(src_w, 1e-6),
        max(1e-6, page_h - 2 * margin_mm - max(reserve_bottom_mm, 0.0) - 2 * pad) / max(src_h, 1e-6),
    )


def to_svg(
    lines_mm: list[Polyline], page_w: float, page_h: float, stroke_mm: float = 0.2,
    stroke_color: str = "black", background_data_uri: str | None = None,
) -> str:
    """Render plot geometry plus an optional display-only background layer.

    ``stroke_color`` is a pen name (black/white/red/blue) resolved through
    the ``LINE_COLORS`` allowlist — unknown values fall back to black so a
    display param can never inject markup. ``background_data_uri`` (a
    ``data:image/...`` URI) is embedded as a fill-only ``<image>`` stretched
    over the full page (``preserveAspectRatio="none"``); with no background
    and white lines a solid dark page rect is emitted instead so white ink
    stays visible in previews. Background layers carry no stroke and are
    ignored by plotters — geometry and stats are unaffected.
    """
    stroke = LINE_COLORS.get(stroke_color, LINE_COLORS["black"])
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{page_w:.2f}mm" '
        f'height="{page_h:.2f}mm" viewBox="0 0 {page_w:.3f} {page_h:.3f}">',
    ]
    if background_data_uri:
        parts.append(
            f'<image href="{background_data_uri}" x="0" y="0" '
            f'width="{page_w:.3f}" height="{page_h:.3f}" '
            'preserveAspectRatio="none"/>',
        )
    elif stroke_color == "white":
        parts.append(
            f'<rect x="0" y="0" width="{page_w:.3f}" height="{page_h:.3f}" fill="#222222"/>',
        )
    parts.append(
        f'<g fill="none" stroke="{stroke}" stroke-width="{stroke_mm}" '
        'stroke-linecap="round" stroke-linejoin="round">',
    )
    for pl in lines_mm:
        if len(pl) < 2:
            continue
        d = f"M {pl[0][0]:.3f} {pl[0][1]:.3f} " + " ".join(
            f"L {x:.3f} {y:.3f}" for x, y in pl[1:]
        )
        parts.append(f'<path d="{d}"/>')
    parts.append("</g></svg>")
    return "\n".join(parts) + "\n"


def build_vpype_command(
    *,
    linemerge_tol: float,
    linesimplify_tol: float,
    linesort_on: bool,
    reloop_tol: float | None,
    page_size: str,
    margin_mm: float,
) -> str:
    """Render the equivalent vpype recipe for transparency/debugging.

    NOTE (review action): this is an *equivalent recipe*, not a byte-reproducing
    command. Running it through real vpype yields the same geometry class but
    not identical bytes — verified divergences are logged in SPEC_V1.md
    ("Implementation & divergence log"): Shapely-backed simplify vs. our
    ring-preserving RDP, greedy+2-opt vs. greedy-only sort, random vs.
    deterministic seam placement. Keep the field name (spec §2.3 mandates it);
    do not present the string as reproducible — see SPEC_V1.md.

    ``reloop_tol=None`` and ``linesort_on=False`` mirror the fast vector
    preview path (P2): stages that were skipped are left out of the recipe.
    """
    segs = ['read --quantization 0.02mm "in.svg"']
    segs.append(f"linemerge --tolerance {linemerge_tol:g}mm")
    segs.append(f"linesimplify --tolerance {linesimplify_tol:g}mm")
    if linesort_on:
        segs.append("linesort")
    if reloop_tol is not None:
        segs.append(f"reloop --tolerance {reloop_tol:g}mm")
    segs.append(f"layout {page_size} --margin {margin_mm:g}mm")
    segs.append('write --color-mode none "out.svg"')
    return " ".join(segs)


__all__ = [
    "build_vpype_command",
    "layout",
    "layout_scale",
    "page_dims_mm",
    "to_svg",
]
