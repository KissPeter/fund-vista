"""Line-generation methods behind a single protocol.

``LineMethod.generate(mask, gray, ctx) -> list[Polyline]`` where a Polyline is
an ordered list of (x, y) points in *source pixel space*. The pipeline scales
to millimetres later (optimize.layout), so methods stay resolution-local and
unit-testable without page math.

Licence note (spec §7): Potrace is GPL-2.0-or-later and vpype-flow-imager is
GPL-3.0. Both are intentionally NOT dependencies here — contour tracing uses
OpenCV (Apache-2.0), centerline tracing uses a NumPy-only Zhang-Suen thinning
pass, curve smoothing is Chaikin corner-cutting in pure Python, and flow uses
a small built-in deterministic field, so the backend stays permissively
licensed (Pillow HPND / NumPy BSD / OpenCV Apache).
If a future v2 wants Potrace fidelity, add a ``PotraceMethod`` class behind
this same protocol and gate it behind an optional extra.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np

log = logging.getLogger(__name__)

Polyline = list[tuple[float, float]]


@dataclass(frozen=True)
class MethodContext:
    threshold: int
    blur_radius: float
    hatch_pitch_mm: float
    contour_simplify: float
    hatch_angle_deg: float = 45.0
    # Hatch pitch already converted to pixels by the pipeline (page-aware).
    hatch_pitch_px: float = 8.0
    # Centerline (skeleton) tracing: drop skeleton branches shorter than this
    # many pixels (thinning spurs from stroke edges/noise). 0 disables pruning.
    centerline_prune_px: int = 4


class LineMethod(Protocol):
    name: str

    def generate(
        self, mask: np.ndarray, gray: np.ndarray, ctx: MethodContext
    ) -> list[Polyline]:
        ...


class ContourMethod:
    """Closed outlines via OpenCV findContours + approxPolyDP.

    ``contour_simplify`` is the approxPolyDP epsilon in pixels: 0 keeps every
    contour point, larger values straighten curves.
    Honoured params (C.2.4): ``threshold``/``blur_radius``/``contrast`` via the mask,
    ``contour_simplify`` as the RDP epsilon. ``hatch_pitch_mm`` is IGNORED —
    a contour tracer has no line spacing. Speck contours under 4 px² are
    dropped as sensor noise (documented, C.2.4c).
    """

    name = "contour"

    def generate(
        self, mask: np.ndarray, gray: np.ndarray, ctx: MethodContext
    ) -> list[Polyline]:
        ink = (mask.astype(np.uint8)) * 255
        contours, _ = cv2.findContours(ink, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
        out: list[Polyline] = []
        eps = max(0.0, float(ctx.contour_simplify))
        for cnt in contours:
            if cv2.contourArea(cnt) < 4.0:
                continue
            if eps > 0:
                approx = cv2.approxPolyDP(cnt, eps, closed=True)
                seq = approx.reshape(-1, 2)
            else:
                seq = cnt.reshape(-1, 2)
            if len(seq) < 2:
                continue
            poly: Polyline = [(float(x), float(y)) for x, y in seq]
            # Close the loop explicitly so downstream length math is exact.
            if poly[0] != poly[-1]:
                poly.append(poly[0])
            out.append(poly)
        log.debug("contour: %d raw contours -> %d polylines", len(contours), len(out))
        if not out:  # blank/thresholded-out image -> visible empty, not an error
            log.debug("contour: empty result (image may be blank at threshold)")
        return out


class HatchMethod:
    """Tonal hatching at ``hatch_angle_deg`` clipped to the ink mask.

    For each hatch line offset by ``hatch_pitch_px`` along the line normal, the
    intersections with dark runs are emitted as segments. Very dark regions
    get a second cross pass at +90° for tone depth — classic pen-plot shading.
    """

    name = "hatch"

    def generate(
        self, mask: np.ndarray, gray: np.ndarray, ctx: MethodContext
    ) -> list[Polyline]:
        h, w = mask.shape[:2]
        pitch = max(2.0, float(ctx.hatch_pitch_px))
        angle = math.radians(float(ctx.hatch_angle_deg) % 180.0)
        cross_angle = angle + math.radians(90.0)
        main = self._hatch_at_angle(mask, w, h, pitch, angle)
        # Cross-hatch only the darkest quartile for depth; cheap and effective.
        dark = gray < max(0, ctx.threshold - 64)
        cross: list[Polyline] = []
        if bool(np.any(dark)):
            cross = self._hatch_at_angle(dark, w, h, pitch * 2.0, cross_angle)
        log.debug("hatch: %d + %d cross segments", len(main), len(cross))
        return main + cross

    @staticmethod
    def _hatch_at_angle(
        mask: np.ndarray, w: int, h: int, pitch: float, angle: float
    ) -> list[Polyline]:
        dx, dy = math.cos(angle), math.sin(angle)
        nx, ny = -dy, dx  # line normal
        # Project corners onto the normal to bound the offset range.
        corners = [(0.0, 0.0), (w, 0.0), (0.0, h), (w, h)]
        projs = [x * nx + y * ny for x, y in corners]
        lo, hi = min(projs), max(projs)
        out: list[Polyline] = []
        # March along each hatch line in 1px steps, collecting dark runs.
        diag = math.hypot(w, h)
        steps = int(diag) + 1
        offset = lo
        while offset <= hi:
            # A point on the line: normal*offset shifted to bbox centre.
            bx, by = nx * offset, ny * offset
            run: Polyline = []
            for s in range(-steps, steps + 1):
                x = bx + dx * s
                y = by + dy * s
                ix, iy = int(round(x)), int(round(y))
                inside = 0 <= ix < w and 0 <= iy < h
                dark = bool(mask[iy, ix]) if inside else False
                if dark:
                    run.append((x, y))
                else:
                    if len(run) >= 2:
                        out.append(run[::2] if len(run) > 64 else run)
                    run = []
            if len(run) >= 2:
                out.append(run[::2] if len(run) > 64 else run)
            offset += pitch
        return out


class FlowMethod:
    """Deterministic organic streamlines modulated by image tone.

    Honoured params (C.2.4): only ``threshold``/``blur_radius``/``contrast``
    (via the mask the pipeline builds) and the image itself are used.
    ``hatch_pitch_mm`` and ``contour_simplify`` are IGNORED by design — the
    field is a fixed-coarse deterministic advection, not line spacing.
    Particles seed on a coarse grid and advect through a cheap analytic field
    (layered sin/cos — no RNG at runtime beyond a fixed seed ordering), dying
    in bright areas. Darker pixels let lines grow longer, so tone emerges from
    line density. Bounded: seeds × max steps are capped so a pathological
    image can't blow up convert latency.
    """

    name = "flow"
    seed_step_px = 28
    max_steps = 220
    step_px = 4.0

    def generate(
        self, mask: np.ndarray, gray: np.ndarray, ctx: MethodContext
    ) -> list[Polyline]:
        h, w = gray.shape[:2]
        tone = (255.0 - gray.astype(np.float32)) / 255.0  # 0 bright .. 1 black
        out: list[Polyline] = []
        for gy in range(self.seed_step_px // 2, h, self.seed_step_px):
            for gx in range(self.seed_step_px // 2, w, self.seed_step_px):
                if tone[gy, gx] < 0.08:
                    continue  # skip paper-white seeds
                line = self._trace(gx, gy, tone, w, h)
                if len(line) >= 4:
                    out.append(line[::2])
                if len(out) >= 600:  # CPU guard
                    log.debug("flow: hit 600-line cap")
                    return out
        log.debug("flow: %d streamlines", len(out))
        return out

    def _trace(
        self, x0: float, y0: float, tone: np.ndarray, w: int, h: int
    ) -> Polyline:
        x, y = float(x0), float(y0)
        line: Polyline = [(x, y)]
        bright_run = 0
        for _ in range(self.max_steps):
            # Analytic pseudo-turbulent field; fully deterministic.
            a = (
                1.6 * math.sin(0.012 * x + 1.7)
                + 1.2 * math.cos(0.015 * y - 0.6)
                + 0.8 * math.sin(0.006 * (x + y))
            )
            x += math.cos(a) * self.step_px
            y += math.sin(a) * self.step_px
            if not (0 <= x < w and 0 <= y < h):
                break
            t = float(tone[int(y), int(x)])
            if t < 0.05:
                bright_run += 1
                if bright_run > 12:
                    break
            else:
                bright_run = 0
            line.append((x, y))
        return line


def _skeleton_neighbors(skel: np.ndarray, x: int, y: int) -> list[tuple[int, int]]:
    h, w = skel.shape[:2]
    out = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and skel[ny, nx]:
                out.append((nx, ny))
    return out


def _collapse_skeleton_ladders(skel: np.ndarray) -> np.ndarray:
    """Collapse 2-px-wide ladder sections to 1 px (vectorized Jacobi passes).

    Zhang-Suen leaves 2-wide bands on curves/diagonals (paired parallel
    chains that would otherwise trace as doubled strokes). Any full 2x2
    foreground block is redundant, so each pass drops every block's
    bottom-right pixel at once (deterministic rail choice) — except pixels
    that are endpoints/bridges (fewer than 3 neighbours) or touch an
    endpoint (which would orphan an attached line tip). Repeat to convergence;
    capped for CPU safety. Never disconnects 1-px lines: those contain no
    full 2x2 block. Checkerboard rails (no full 2x2) are left for the
    echo-rail drop at trace level.
    """
    skel = skel.copy()
    if skel.shape[0] < 2 or skel.shape[1] < 2:
        return skel
    shifts = (
        (-1, -1), (-1, 0), (-1, 1),
        (0, -1), (0, 1),
        (1, -1), (1, 0), (1, 1),
    )
    for _ in range(10):
        s = skel.astype(np.uint8)
        full = s[:-1, :-1] & s[:-1, 1:] & s[1:, :-1] & s[1:, 1:]
        if not bool(np.any(full)):
            break
        h, w = s.shape
        deg = np.zeros((h, w), dtype=np.uint8)
        for dy, dx in shifts:
            deg[
                max(0, -dy): h + min(0, -dy),
                max(0, -dx): w + min(0, -dx),
            ] += s[
                max(0, dy): h + min(0, dy),
                max(0, dx): w + min(0, dx),
            ]
        endpoints = skel & (deg == 1)
        ep_dil = endpoints.copy()
        for dy, dx in shifts:
            ep_dil[
                max(0, -dy): h + min(0, -dy),
                max(0, -dx): w + min(0, -dx),
            ] |= endpoints[
                max(0, dy): h + min(0, dy),
                max(0, dx): w + min(0, dx),
            ]
        drop = np.zeros((h, w), dtype=bool)
        drop[1:, 1:] |= full  # bottom-right pixel of every full 2x2 block
        drop &= (deg >= 3) & ~ep_dil
        if not bool(np.any(drop)):
            break
        skel[drop] = False
    return skel


def _skeleton_degree_map(skel: np.ndarray) -> np.ndarray:
    """8-neighbour foreground count per pixel (vectorized)."""
    s = skel.astype(np.uint8)
    h, w = s.shape
    deg = np.zeros((h, w), dtype=np.uint8)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            deg[
                max(0, -dy): h + min(0, -dy),
                max(0, -dx): w + min(0, -dx),
            ] += s[
                max(0, dy): h + min(0, dy),
                max(0, dx): w + min(0, dx),
            ]
    return deg


def _prune_skeleton_spurs(skel: np.ndarray, min_len_px: float) -> np.ndarray:
    """Peel short end-branches (thinning spurs) off a skeleton in place-copy.

    Thinning grows a spur wherever the stroke edge wobbles; each spur ends in
    an endpoint, so: walk every endpoint to its junction and delete the walk
    when shorter than ``min_len_px``. Repeat until stable (peeling can expose
    spur-on-spur). Junction pixels themselves are kept so real branches stay
    connected. A spur-free ring is then a pure loop and traces as one stroke.
    """
    skel = skel.copy()
    h, w = skel.shape[:2]
    if min_len_px <= 0 or not bool(np.any(skel)):
        return skel
    for _ in range(25):  # iteration cap; line art settles in ~2-3 passes
        removed_any = False
        deg = _skeleton_degree_map(skel)
        ends = list(zip(
            *np.where(skel & (deg == 1)), strict=True,
        ))
        # np.where returns (rows, cols) — flip to (x, y).
        ends = [(int(x), int(y)) for y, x in ends]
        claimed: set[tuple[int, int]] = set()
        for sx, sy in ends:
            if not skel[sy, sx] or (sx, sy) in claimed:
                continue
            spur = [(sx, sy)]
            px, py = sx, sy
            cx, cy = _skeleton_neighbors(skel, sx, sy)[0]
            while True:
                spur.append((cx, cy))
                nbrs = [
                    q for q in _skeleton_neighbors(skel, cx, cy) if q != (px, py)
                ]
                if len(nbrs) != 1:
                    break  # junction (or dead end): stop, keep this pixel
                px, py = cx, cy
                cx, cy = nbrs[0]
                if len(spur) > h * w:
                    break
            claimed.update(spur)
            # Length in px; the final pixel is the junction (kept) or, when
            # the walk never met one, the far endpoint of a tiny component.
            kept_tail = 1 if len(_skeleton_neighbors(skel, *spur[-1])) != 1 else 0
            seg_len = sum(
                math.hypot(spur[i + 1][0] - spur[i][0], spur[i + 1][1] - spur[i][1])
                for i in range(len(spur) - 1)
            )
            if seg_len < min_len_px:
                for q in spur[: len(spur) - kept_tail]:
                    if skel[q[1], q[0]]:
                        skel[q[1], q[0]] = False
                        removed_any = True
        if not removed_any:
            break
    return skel


def _skeletonize_zhang_suen(ink: np.ndarray) -> np.ndarray:
    """Thin a binary ink image to a 1-px skeleton (Zhang-Suen, numpy only).

    ``ink`` is a bool/uint8 HxW array (True/ink). Returns a bool HxW skeleton.
    Pure NumPy — no scikit-image or opencv-contrib dependency — so the
    backend keeps its permissive-licence footprint (spec §7). Iterations are
    capped for CPU safety; line art converges far below the cap.
    """
    skel = (ink > 0) if ink.dtype != bool else ink.copy()
    skel = skel.copy()
    h, w = skel.shape[:2]
    if h == 0 or w == 0:
        return skel
    # Pad so the 3x3 neighbourhood read never goes out of bounds.
    img = np.pad(skel.astype(np.uint8), 1)
    # Strokes wider than ~2x the cap are fills, not linework: stop early and
    # trace the shrunken blob's outline as a loop (graceful fallback; solid
    # fills want the contour method — see the class docstring).
    for _ in range(120):
        changed = False
        for step in (0, 1):
            p = img[1:-1, 1:-1]
            # 8 neighbours, clockwise from north.
            p2 = img[:-2, 1:-1]
            p3 = img[:-2, 2:]
            p4 = img[1:-1, 2:]
            p5 = img[2:, 2:]
            p6 = img[2:, 1:-1]
            p7 = img[2:, :-2]
            p8 = img[1:-1, :-2]
            p9 = img[:-2, :-2]
            nbrs = p2 + p3 + p4 + p5 + p6 + p7 + p8 + p9
            # 0->1 transitions in the ordered ring p2..p9,p2.
            ring_prev = np.stack([p9, p2, p3, p4, p5, p6, p7, p8], axis=0)
            ring_next = np.stack([p2, p3, p4, p5, p6, p7, p8, p9], axis=0)
            transitions = np.sum(
                (ring_prev == 0) & (ring_next == 1), axis=0,
            )
            if step == 0:
                cond = (p2 * p4 * p6 == 0) & (p4 * p6 * p8 == 0)
            else:
                cond = (p2 * p4 * p8 == 0) & (p2 * p6 * p8 == 0)
            kill = (
                (p == 1)
                & (nbrs >= 2) & (nbrs <= 6)
                & (transitions == 1)
                & cond
            )
            if bool(np.any(kill)):
                changed = True
                sub = img[1:-1, 1:-1]
                sub[kill] = 0
        if not changed:
            break
    return img[1:-1, 1:-1].astype(bool)


def _drop_echo_rails(
    branches: list[list[tuple[int, int]]], max_gap_px: float = 2.5,
    max_echo_len_px: float = 128.0,
) -> list[list[tuple[int, int]]]:
    """Drop doubled-trace rails: a branch that leaves a stroke and rejoins it.

    On curves/diagonals thinning can emit two parallel 1-px rails (a
    checkerboard ladder with no full 2x2 block for the collapser to grip).
    The spare rail is geometrically distinctive: short, open, both endpoints
    touching the parent stroke, and hugging it along its whole length. Real
    linework never looks like that — a chord or crossing arm diverges from
    its parent (large max deviation), a T-arm has a free endpoint, and genuine
    loops are closed (start == end, always kept).
    """
    if len(branches) < 2:
        return branches

    def path_len(pts: list[tuple[int, int]]) -> float:
        return sum(
            math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
            for i in range(len(pts) - 1)
        )

    def bbox(pts: list[tuple[int, int]]) -> tuple[int, int, int, int]:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    def touches(pt: tuple[int, int], other: list[tuple[int, int]]) -> bool:
        return any(
            math.hypot(pt[0] - q[0], pt[1] - q[1]) <= max_gap_px for q in other
        )

    lens = [path_len(b) for b in branches]
    boxes = [bbox(b) for b in branches]
    drop = [False] * len(branches)
    for i, cand in enumerate(branches):
        if lens[i] > max_echo_len_px or len(cand) < 2:
            continue
        if math.hypot(cand[0][0] - cand[-1][0], cand[0][1] - cand[-1][1]) <= 1.5:
            continue  # closed loop: genuine hole/ring, always keep
        for j, parent in enumerate(branches):
            if i == j or drop[j] or lens[j] < lens[i]:
                continue
            bx0, by0, bx1, by1 = boxes[j]
            cx0, cy0, cx1, cy1 = boxes[i]
            if (
                cx0 > bx1 + max_gap_px or cx1 < bx0 - max_gap_px
                or cy0 > by1 + max_gap_px or cy1 < by0 - max_gap_px
            ):
                continue
            if not (touches(cand[0], parent) and touches(cand[-1], parent)):
                continue
            worst = 0.0
            for pt in cand:
                d = min(
                    math.hypot(pt[0] - q[0], pt[1] - q[1]) for q in parent
                )
                if d > worst:
                    worst = d
                    if worst > max_gap_px:
                        break
            if worst <= max_gap_px:
                drop[i] = True
                break
    return [b for b, d in zip(branches, drop) if not d]


class CenterlineMethod:
    """Monoline centerlines via skeletonization + skeleton-graph walk.

    For pen/ink linework the contour tracer follows *both* edges of every
    stroke band (the "wheel/curve mess": doubled outlines, faceted curves).
    Thinning collapses each stroke to its 1-px centerline first, so there is
    a single line to follow — the skimage.skeletonize idea, implemented here
    with the NumPy-only Zhang-Suen pass above (no new dependency).

    Honoured params: ``threshold``/``blur_radius``/``contrast`` via the mask,
    ``contour_simplify`` as the RDP epsilon applied to each traced branch,
    ``centerline_prune_px`` drops thinning-spur branches shorter than this.
    ``hatch_pitch_mm`` is IGNORED (no line spacing involved). Solid fills
    thin to web-like medial webs rather than outlines — pair with ``contour``
    (multi-select) when both are wanted.
    """

    name = "centerline"

    def generate(
        self, mask: np.ndarray, gray: np.ndarray, ctx: MethodContext
    ) -> list[Polyline]:
        ink = np.asarray(mask, dtype=bool)
        if not bool(np.any(ink)):
            return []  # blank/thresholded-out image -> visible empty
        if int(np.count_nonzero(ink)) < 8:
            return []
        # Crop to the ink bounding box (+1 px): empty scan margins cost
        # full-frame array passes in every thinning iteration otherwise.
        ys, xs = np.where(ink)
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        crop = ink[y0:y1, x0:x1]
        skel = _skeletonize_zhang_suen(crop)
        if not bool(np.any(skel)):
            return []
        prune = max(0, int(getattr(ctx, "centerline_prune_px", 4)))
        # Ladders first (2-wide bands unzip to one rail), then spurs: a spur
        # endpoint walk must never eat into the parent stroke and splinter it.
        skel = _collapse_skeleton_ladders(skel)
        if prune > 0:
            skel = _prune_skeleton_spurs(skel, float(prune))
        if not bool(np.any(skel)):
            return []
        branches = self._trace_skeleton(skel)
        branches = _drop_echo_rails(branches)
        # Back to source pixel space.
        branches = [
            [(x + x0, y + y0) for x, y in branch] for branch in branches
        ]

        def branch_len_px(pts: list[tuple[int, int]]) -> float:
            return sum(
                math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
                for i in range(len(pts) - 1)
            )

        out: list[Polyline] = []
        eps = max(0.0, float(ctx.contour_simplify))
        for pts in branches:
            if len(pts) < 2:
                continue
            if prune > 0 and branch_len_px(pts) < prune:
                continue
            poly: Polyline = [(float(x), float(y)) for x, y in pts]
            if eps > 0 and len(poly) >= 3:
                # Rings must stay rings: approxPolyDP(closed=False) opens the
                # seam at an arbitrary vertex, so simplify closed branches as
                # closed and re-append the closure point explicitly.
                closed = math.hypot(
                    poly[0][0] - poly[-1][0], poly[0][1] - poly[-1][1]
                ) <= 1.5
                body = poly[:-1] if closed else poly
                if len(body) >= 3:
                    cnt = np.array(
                        [[x, y] for x, y in body], dtype=np.float32,
                    ).reshape(-1, 1, 2)
                    approx = cv2.approxPolyDP(cnt, eps, closed=closed)
                    seq = approx.reshape(-1, 2)
                    if len(seq) >= 2:
                        poly = [(float(x), float(y)) for x, y in seq]
                        if closed:
                            poly.append(poly[0])
            if len(poly) >= 2:
                out.append(poly)
        log.debug("centerline: %d branches -> %d polylines", len(branches), len(out))
        return out

    @staticmethod
    def _trace_skeleton(skel: np.ndarray) -> list[list[tuple[int, int]]]:
        """Walk the 1-px skeleton into long chains (endpoint to endpoint).

        At each step the straightest unvisited 8-neighbour wins, so staircase
        pixels (3 neighbours from discretization, not true forks) are walked
        straight through instead of splitting the line — a ring traces as one
        closed loop, not dozens of spurs. A final step onto an already-traced
        junction pixel is allowed so branch tips touch their junction (the
        downstream linemerge then rejoins them exactly).
        """
        h, w = skel.shape[:2]

        def neighbours(x: int, y: int) -> list[tuple[int, int]]:
            out = []
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h and skel[ny, nx]:
                        out.append((nx, ny))
            return out

        visited = np.zeros((h, w), dtype=bool)
        branches: list[list[tuple[int, int]]] = []

        def walk(sx: int, sy: int) -> list[tuple[int, int]]:
            """Extend a chain from (sx, sy) until no unvisited neighbour."""
            chain = [(sx, sy)]
            visited[sy, sx] = True
            px, py = sx, sy
            # Incoming direction (for straightness); none at the start.
            dx0, dy0 = 0, 0
            cx, cy = sx, sy
            while True:
                cands = [
                    q for q in neighbours(cx, cy)
                    if not visited[q[1], q[0]] and q != (px, py)
                ]
                if not cands:
                    # One closing step onto traced ground (junction/loop seam
                    # contact) so the tip touches instead of stopping 1 px shy.
                    closers = [
                        q for q in neighbours(cx, cy)
                        if q != (px, py) and (q == (sx, sy) or len(chain) < 3 or q not in chain[-3:])
                    ]
                    if closers and (cx, cy) != (sx, sy):
                        chain.append(closers[0])
                    return chain
                if dx0 == 0 and dy0 == 0:
                    nxt = cands[0]
                else:
                    nxt = min(
                        cands,
                        key=lambda q: (
                            (q[0] - cx) * dy0 - (q[1] - cy) * dx0
                        ) ** 2
                        - ((q[0] - cx) * dx0 + (q[1] - cy) * dy0),
                    )
                dx0, dy0 = nxt[0] - cx, nxt[1] - cy
                px, py = cx, cy
                cx, cy = nxt
                chain.append((cx, cy))
                visited[cy, cx] = True
                if len(chain) > h * w:  # safety: never loop forever
                    return chain

        # Pass 1: endpoints (and isolated pixels) first — every open stroke
        # grows end-to-end before loops claim shared junctions. Start coords
        # come from a vectorized degree map, not a Python full-frame scan.
        deg = _skeleton_degree_map(skel)
        ys, xs = np.where(skel & (deg != 2))
        for x, y in zip((int(v) for v in xs), (int(v) for v in ys)):
            if visited[y, x]:
                continue
            branch = walk(x, y)
            if len(branch) >= 2:
                branches.append(branch)
        # Pass 2: whatever is left are closed loops (no endpoints).
        ys, xs = np.where(skel & ~visited)
        for x, y in zip((int(v) for v in xs), (int(v) for v in ys)):
            if visited[y, x]:
                continue
            loop = walk(x, y)
            if len(loop) >= 2:
                if math.hypot(loop[-1][0] - loop[0][0], loop[-1][1] - loop[0][1]) <= 1.5:
                    loop.append(loop[0])  # close the ring explicitly
                branches.append(loop)
        return branches


METHOD_REGISTRY: dict[str, LineMethod] = {
    "contour": ContourMethod(),
    "centerline": CenterlineMethod(),
    "hatch": HatchMethod(),
    "flow": FlowMethod(),
}
