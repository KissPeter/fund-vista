"""vpype-equivalent cleanup in pure Python (MIT-safe, no binary dep).

Stage order mirrors the Drawscape/vpype chain from spec §3.3 so the returned
``vpype_command`` string stays an honest description of what ran::

    layout -> quantize (= read --quantization) -> linemerge -> linesimplify
        -> linesort -> reloop -> write

All geometry here is in millimetres *after* layout, except linemerge/simplify
tolerances which arrive in mm and are applied post-layout (same as vpype,
which works in final units). Points/segments before/after are counted around
simplify+merge so ``stats`` reflects real savings.
"""

from __future__ import annotations

import logging
import math

from backend.penplot.config import PAGE_SIZES_MM
from backend.penplot.methods import Polyline

log = logging.getLogger(__name__)


def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def polyline_length(pl: Polyline) -> float:
    return sum(dist(pl[i], pl[i + 1]) for i in range(len(pl) - 1))


def count_points(lines: list[Polyline]) -> int:
    return sum(len(pl) for pl in lines)


# -- quantize (vpype `read --quantization`) ---------------------------------

def quantize(lines: list[Polyline], step: float) -> list[Polyline]:
    """Snap every coordinate to a ``step`` grid (mm), dropping degenerate lines.

    Runs right after layout so — like vpype — all downstream tolerances operate
    on quantized input. Consecutive duplicates created by snapping are removed;
    polylines collapsing below 2 points are dropped (stats stay consistent).
    """
    if step <= 0:
        return [list(pl) for pl in lines]
    out: list[Polyline] = []
    for pl in lines:
        snapped = [(round(x / step) * step, round(y / step) * step) for x, y in pl]
        deduped = [snapped[0]]
        for pt in snapped[1:]:
            if dist(pt, deduped[-1]) > 1e-12:
                deduped.append(pt)
        if len(deduped) >= 2:
            out.append(deduped)
    log.debug(
        "quantize: %d -> %d pts (step=%.3fmm)", count_points(lines), count_points(out), step
    )
    return out


# -- linemerge ----------------------------------------------------------

def linemerge(lines: list[Polyline], tol: float) -> list[Polyline]:
    """Greedily join endpoint-touching polylines (any orientation)."""
    if tol <= 0 or len(lines) < 2:
        return [list(pl) for pl in lines]
    work = [list(pl) for pl in lines if len(pl) >= 2]
    changed = True
    while changed:
        changed = False
        used = [False] * len(work)
        merged: list[Polyline] = []
        for i, a in enumerate(work):
            if used[i]:
                continue
            used[i] = True
            cur = list(a)
            extended = True
            while extended:
                extended = False
                for j, b in enumerate(work):
                    if used[j]:
                        continue
                    joined: Polyline | None = None
                    if dist(cur[-1], b[0]) <= tol:
                        joined = cur + list(b)[1:]
                    elif dist(cur[-1], b[-1]) <= tol:
                        joined = cur + list(reversed(b))[1:]
                    elif dist(cur[0], b[-1]) <= tol:
                        joined = list(b) + cur[1:]
                    elif dist(cur[0], b[0]) <= tol:
                        joined = list(reversed(b)) + cur[1:]
                    if joined is not None:
                        cur = joined
                        used[j] = True
                        extended = True
                        changed = True
                        break
            merged.append(cur)
        work = merged
    log.debug("linemerge: %d -> %d strokes (tol=%.3fmm)", len(lines), len(work), tol)
    return work


# -- linesimplify (Ramer–Douglas–Peucker) --------------------------------

def _rdp_open(points: Polyline, eps: float) -> Polyline:
    """RDP core for open polylines (endpoints must differ)."""
    if len(points) < 3 or eps <= 0:
        return list(points)
    x0, y0 = points[0]
    x1, y1 = points[-1]
    dx, dy = x1 - x0, y1 - y0
    denom = math.hypot(dx, dy) or 1e-12
    best_i, best_d = -1, 0.0
    for i in range(1, len(points) - 1):
        px, py = points[i]
        d = abs(dy * px - dx * py + x1 * y0 - y1 * x0) / denom
        if d > best_d:
            best_d, best_i = d, i
    if best_d > eps:
        left = _rdp_open(points[: best_i + 1], eps)
        right = _rdp_open(points[best_i:], eps)
        return left[:-1] + right
    return [points[0], points[-1]]


def _rdp(points: Polyline, eps: float) -> Polyline:
    """RDP that also handles closed rings.

    A closed ring has a zero-length chord, so the open-polyline distance test
    degenerates (every distance reads as 0 and the ring collapses to two
    identical points). Break the ring open at the vertex farthest from vertex
    0 first — endpoints are then far apart and simplification is exact.
    """
    if len(points) < 3 or eps <= 0:
        return list(points)
    if dist(points[0], points[-1]) <= 1e-9:
        body = points[:-1]
        if len(body) < 3:
            return list(points)
        k = max(range(len(body)), key=lambda i: dist(body[i], body[0]))
        # Cut the ring at k into an OPEN chain (endpoints are ring-adjacent),
        # simplify the chain, then re-close it.
        chain = body[k:] + body[:k]
        simp = _rdp_open(chain, eps)
        if dist(simp[0], simp[-1]) > 1e-9:
            simp.append(simp[0])
        return simp
    return _rdp_open(points, eps)


def linesimplify(lines: list[Polyline], tol: float) -> list[Polyline]:
    if tol <= 0:
        return [list(pl) for pl in lines]
    out = [_rdp(pl, tol) for pl in lines]
    log.debug(
        "linesimplify: %d -> %d pts (tol=%.3fmm)",
        count_points(lines), count_points(out), tol,
    )
    return out


# -- linesort (greedy nearest-neighbour incl. reversal) ------------------

def linesort(lines: list[Polyline]) -> list[Polyline]:
    """Reorder (and maybe reverse) strokes to minimise pen-up travel."""
    if len(lines) < 2:
        return [list(pl) for pl in lines]
    remaining = [list(pl) for pl in lines]
    ordered = [remaining.pop(0)]
    pen = ordered[0][-1]
    while remaining:
        best_j, best_rev, best_d = -1, False, math.inf
        for j, cand in enumerate(remaining):
            d_fwd = dist(pen, cand[0])
            d_rev = dist(pen, cand[-1])
            if d_fwd <= d_rev and d_fwd < best_d:
                best_j, best_rev, best_d = j, False, d_fwd
            elif d_rev < best_d:
                best_j, best_rev, best_d = j, True, d_rev
        nxt = remaining.pop(best_j)
        if best_rev:
            nxt = list(reversed(nxt))
        ordered.append(nxt)
        pen = nxt[-1]
    return ordered


# -- reloop --------------------------------------------------------------

def reloop(lines: list[Polyline], tol: float) -> list[Polyline]:
    """Rotate closed loops so the seam sits nearest the previous pen position."""
    if tol < 0:
        return lines
    out: list[Polyline] = []
    prev_end: tuple[float, float] | None = None
    for pl in lines:
        if len(pl) >= 4 and dist(pl[0], pl[-1]) <= max(tol, 1e-9):
            body = pl[:-1]  # drop duplicated closure point for rotation
            anchor = prev_end if prev_end is not None else body[0]
            k = min(range(len(body)), key=lambda i: dist(body[i], anchor))
            rotated = body[k:] + body[:k] + [body[k]]
            out.append(rotated)
            prev_end = rotated[-1]
        else:
            out.append(pl)
            prev_end = pl[-1]
    return out


# -- layout ---------------------------------------------------------------

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
) -> tuple[list[Polyline], float, float]:
    """Scale pixel polylines into the margined page rect (mm). Returns (lines, W, H).

    ``reserve_bottom_mm`` keeps a strip above the bottom margin free (the
    title-block label zone) — artwork centers in the remaining area and
    bottoms out exactly where the label divider will sit.
    """
    page_w, page_h = page_dims_mm(size, orientation, margin_mm)
    draw_w = max(1e-6, page_w - 2 * margin_mm)
    draw_h = max(1e-6, page_h - 2 * margin_mm - max(reserve_bottom_mm, 0.0))
    scale = min(draw_w / max(src_w, 1e-6), draw_h / max(src_h, 1e-6))
    ox = margin_mm + (draw_w - src_w * scale) / 2.0
    oy = margin_mm + (draw_h - src_h * scale) / 2.0
    out = [
        [(ox + x * scale, oy + y * scale) for x, y in pl]
        for pl in lines_px
    ]
    return out, page_w, page_h


def layout_scale(src_w: float, src_h: float, size: str, orientation: str, margin_mm: float, reserve_bottom_mm: float = 0.0) -> float:
    page_w, page_h = page_dims_mm(size, orientation, margin_mm)
    return min(
        max(1e-6, page_w - 2 * margin_mm) / max(src_w, 1e-6),
        max(1e-6, page_h - 2 * margin_mm - max(reserve_bottom_mm, 0.0)) / max(src_h, 1e-6),
    )


# -- svg -------------------------------------------------------------------

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
    from backend.penplot.backgrounds import LINE_COLORS

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
    reloop_tol: float,
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
    """
    segs = ['read --quantization 0.02mm "in.svg"']
    segs.append(f"linemerge --tolerance {linemerge_tol:g}mm")
    segs.append(f"linesimplify --tolerance {linesimplify_tol:g}mm")
    if linesort_on:
        segs.append("linesort")
    segs.append(f"reloop --tolerance {reloop_tol:g}mm")
    segs.append(f"layout {page_size} --margin {margin_mm:g}mm")
    segs.append('write --color-mode none "out.svg"')
    return " ".join(segs)
