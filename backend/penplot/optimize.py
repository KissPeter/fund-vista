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
    """Greedily join endpoint-touching polylines (any orientation).

    Semantics identical to the original: seed each merged stroke from the
    first unused line in input order, then repeatedly absorb the *first
    unused* line that touches the growing stroke at either end (four
    orientation checks in a fixed priority). Neighbor lookup now uses a
    spatial hash over endpoints instead of an O(n) scan per step — the old
    version was O(n^2)/O(n^3) on dense diagrams (a 5000-stroke airport SVG
    spent ~10 s merging; the hash drops that to milliseconds).
    """
    if tol <= 0 or len(lines) < 2:
        return [list(pl) for pl in lines]
    work = [list(pl) for pl in lines if len(pl) >= 2]
    if len(work) < 2:
        return work
    n = len(work)

    def cell(p: tuple[float, float]) -> tuple[int, int]:
        # Cell size = tol: points close enough to join (distance <= tol) are
        # always within 2 cells of each other, so a 5x5 neighborhood is safe;
        # the exact distance test below filters approximate from true matches.
        return (math.floor(p[0] / tol), math.floor(p[1] / tol))

    buckets: dict[tuple[int, int], set[int]] = {}
    for i, pl in enumerate(work):
        for end in (pl[0], pl[-1]):
            buckets.setdefault(cell(end), set()).add(i)
    used = [False] * n
    merged: list[Polyline] = []
    for i in range(n):
        if used[i]:
            continue
        used[i] = True
        cur = list(work[i])
        while True:
            candidates: set[int] = set()
            for end in (cur[0], cur[-1]):
                cx, cy = cell(end)
                for dx in range(-2, 3):
                    for dy in range(-2, 3):
                        hit = buckets.get((cx + dx, cy + dy))
                        if hit:
                            candidates |= hit
            pick = -1
            for j in sorted(candidates):
                if used[j]:
                    continue
                b = work[j]
                if dist(cur[-1], b[0]) <= tol:
                    cur = cur + list(b)[1:]
                elif dist(cur[-1], b[-1]) <= tol:
                    cur = cur + list(reversed(b))[1:]
                elif dist(cur[0], b[-1]) <= tol:
                    cur = list(b) + cur[1:]
                elif dist(cur[0], b[0]) <= tol:
                    cur = list(reversed(b)) + cur[1:]
                else:
                    continue
                used[j] = True
                pick = j
                break
            if pick < 0:
                break
        merged.append(cur)
    log.debug("linemerge: %d -> %d strokes (tol=%.3fmm)", len(lines), len(merged), tol)
    return merged


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


# -- curvesmooth (Chaikin corner-cutting) ---------------------------------
# Stand-in for potrace -a/alphamax + --opttolerance and vtracer's spline
# mode: rounds faceted contour/centerline corners into smooth curves with no
# new dependency (pure Python, operates post-layout in mm). 0 iterations is
# identity (off). Runs between linemerge and linesimplify so the RDP pass
# can still drop any redundant points the smoothing adds.

def _chaikin_once(points: Polyline) -> Polyline:
    if len(points) < 3:
        return list(points)
    closed = dist(points[0], points[-1]) <= 1e-9
    body = points[:-1] if closed else points
    if len(body) < 3:
        return list(points)
    out: Polyline = []
    n = len(body)
    for i in range(n if closed else n - 1):
        p0 = body[i]
        p1 = body[(i + 1) % n] if closed else body[i + 1]
        if i == 0 and not closed:
            out.append(p0)  # keep open endpoints fixed
        q = (0.75 * p0[0] + 0.25 * p1[0], 0.75 * p0[1] + 0.25 * p1[1])
        r = (0.25 * p0[0] + 0.75 * p1[0], 0.25 * p0[1] + 0.75 * p1[1])
        out += [q, r]
    if closed:
        out.append(out[0])
    else:
        out.append(body[-1])
    return out


def curvesmooth(lines: list[Polyline], iterations: int) -> list[Polyline]:
    """Round polyline corners via Chaikin corner-cutting (mm space)."""
    iters = max(0, int(iterations))
    if iters <= 0:
        return [list(pl) for pl in lines]
    out = [list(pl) for pl in lines]
    for _ in range(min(iters, 5)):  # CPU/size guard: each pass ~doubles points
        out = [_chaikin_once(pl) for pl in out]
    log.debug(
        "curvesmooth: %d -> %d pts (iters=%d)",
        count_points(lines), count_points(out), iters,
    )
    return out


# -- linesort (greedy nearest-neighbour incl. reversal) ------------------

def _cell_min_dist(pen: tuple[float, float], cell: tuple[int, int], cell_size: float) -> float:
    """Distance from ``pen`` to the nearest corner of the cell rectangle.

    No point inside the cell can be closer to the pen than this, which is
    what lets the ring search stop early without missing neighbors.
    """
    x0, x1 = cell[0] * cell_size, (cell[0] + 1) * cell_size
    y0, y1 = cell[1] * cell_size, (cell[1] + 1) * cell_size
    dx = 0.0 if x0 <= pen[0] <= x1 else min(abs(pen[0] - x0), abs(pen[0] - x1))
    dy = 0.0 if y0 <= pen[1] <= y1 else min(abs(pen[1] - y0), abs(pen[1] - y1))
    return math.hypot(dx, dy)


def _linesort_nearest(
    pen: tuple[float, float],
    work: list[Polyline],
    available: set[int],
    buckets: dict[tuple[int, int], set[int]],
    cell,
    cell_size: float,
) -> tuple[int, bool]:
    """Greedy nearest-neighbour selection with the legacy tie-break rules.

    Returns ``(index, reverse)`` for the first-available stroke at the
    minimum end-distance (earliest index wins distance ties; forward
    preferred within the winner). A bounded ring expansion over the spatial
    hash finds the winner in sparse-searching time; when a drawing is too
    sparse for the grid to pay off, the exact legacy linear scan is used
    instead (identical selection, just slower on that rare case).
    """
    best_d = math.inf
    best_j = -1
    best_rev = False
    cx, cy = cell(pen)
    scanned = 0
    r = 0
    budget = max(128, 16 * int(math.sqrt(max(len(available), 1))))
    complete = False
    while True:
        if r == 0:
            coords = ((cx, cy),)
        else:
            coords = (
                [(cx + dx, cy - r) for dx in range(-r, r + 1)]
                + [(cx + dx, cy + r) for dx in range(-r, r + 1)]
                + [(cx - r, cy + dy) for dy in range(-r + 1, r)]
                + [(cx + r, cy + dy) for dy in range(-r + 1, r)]
            )
        ring_min = math.inf
        for X, Y in coords:
            scanned += 1
            ring_min = min(ring_min, _cell_min_dist(pen, (X, Y), cell_size))
            hits = buckets.get((X, Y))
            if not hits:
                continue
            for j in hits:
                if j not in available:
                    continue
                cand = work[j]
                d_fwd = dist(pen, cand[0])
                d_rev = dist(pen, cand[-1])
                d, rev = (d_rev, True) if d_rev < d_fwd else (d_fwd, False)
                if d < best_d or (d == best_d and j < best_j):
                    best_d, best_j, best_rev = d, j, rev
        if best_j >= 0 and ring_min > best_d:
            complete = True
            break
        if scanned > budget:
            break
        r += 1
    if not complete:
        # Grid search ran out of budget (sparse defence) — reproduce the
        # exact legacy selection, identical tie-breaks. Correctness never
        # depends on the hash; it is only an accelerator for the common,
        # dense case.
        best_d = math.inf
        best_j = -1
        best_rev = False
        for j in sorted(available):
            cand = work[j]
            d_fwd = dist(pen, cand[0])
            d_rev = dist(pen, cand[-1])
            if d_fwd <= d_rev and d_fwd < best_d:
                best_j, best_rev, best_d = j, False, d_fwd
            elif d_rev < best_d:
                best_j, best_rev, best_d = j, True, d_rev
    return best_j, best_rev


def linesort(lines: list[Polyline]) -> list[Polyline]:
    """Reorder (and maybe reverse) strokes to minimise pen-up travel.

    Same greedy nearest-neighbour rule as before — earliest remaining stroke
    at the minimum end-distance wins, forward first on a tie — but the scan
    hits a spatial hash with ring expansion instead of every remaining
    stroke (old worst case ~O(n^2), ~1.7 s on 5000 strokes).
    """
    if len(lines) < 2:
        return [list(pl) for pl in lines]
    work = [list(pl) for pl in lines]
    n = len(work)
    # Grid scale from the drawing's span so the nearest stroke usually sits
    # in the pen's own cell (ring radius 0-1) without exploding on huge,
    # on-page spans.
    xs = [p[0] for pl in work for p in pl]
    ys = [p[1] for pl in work for p in pl]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
    cell_size = min(8.0, max(0.5, span / 200.0))

    def cell(p: tuple[float, float]) -> tuple[int, int]:
        return (math.floor(p[0] / cell_size), math.floor(p[1] / cell_size))

    buckets: dict[tuple[int, int], set[int]] = {}
    for i, pl in enumerate(work):
        for end in (pl[0], pl[-1]):
            buckets.setdefault(cell(end), set()).add(i)
    available = set(range(n))
    ordered: list[Polyline] = [work[0]]
    available.remove(0)
    pen = ordered[0][-1]
    while available:
        j, rev = _linesort_nearest(pen, work, available, buckets, cell, cell_size)
        nxt = list(reversed(work[j])) if rev else list(work[j])
        available.discard(j)
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


def layout_scale(src_w: float, src_h: float, size: str, orientation: str, margin_mm: float, reserve_bottom_mm: float = 0.0, padding_mm: float = 0.0) -> float:
    page_w, page_h = page_dims_mm(size, orientation, margin_mm)
    pad = max(float(padding_mm), 0.0)
    return min(
        max(1e-6, page_w - 2 * margin_mm - 2 * pad) / max(src_w, 1e-6),
        max(1e-6, page_h - 2 * margin_mm - max(reserve_bottom_mm, 0.0) - 2 * pad) / max(src_h, 1e-6),
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
