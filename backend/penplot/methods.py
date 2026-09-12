"""Line-generation methods behind a single protocol.

``LineMethod.generate(mask, gray, ctx) -> list[Polyline]`` where a Polyline is
an ordered list of (x, y) points in *source pixel space*. The pipeline scales
to millimetres later (optimize.layout), so methods stay resolution-local and
unit-testable without page math.

Licence note (spec §7): Potrace is GPL-2.0-or-later and vpype-flow-imager is
GPL-3.0. Both are intentionally NOT dependencies here — contour tracing uses
OpenCV (Apache-2.0) and flow uses a small built-in deterministic field, so the
backend stays permissively licensed (Pillow HPND / NumPy BSD / OpenCV Apache).
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
    # Hatch pitch already converted to pixels by the pipeline (page-aware).
    hatch_pitch_px: float = 8.0


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
    Honoured params (C.2.4): ``threshold``/``blur_radius`` via the mask,
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
    """Single-angle (45°) tonal hatching clipped to the ink mask.

    For each hatch line offset by ``hatch_pitch_px`` along the line normal, the
    intersections with dark runs are emitted as segments. Very dark regions
    get a second cross pass (135°) for tone depth — classic pen-plot shading.
    """

    name = "hatch"
    angle_rad = math.radians(45.0)
    cross_angle_rad = math.radians(135.0)

    def generate(
        self, mask: np.ndarray, gray: np.ndarray, ctx: MethodContext
    ) -> list[Polyline]:
        h, w = mask.shape[:2]
        pitch = max(2.0, float(ctx.hatch_pitch_px))
        main = self._hatch_at_angle(mask, w, h, pitch, self.angle_rad)
        # Cross-hatch only the darkest quartile for depth; cheap and effective.
        dark = gray < max(0, ctx.threshold - 64)
        cross: list[Polyline] = []
        if bool(np.any(dark)):
            cross = self._hatch_at_angle(dark, w, h, pitch * 2.0, self.cross_angle_rad)
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

    Honoured params (C.2.4): only ``threshold``/``blur_radius``
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


METHOD_REGISTRY: dict[str, LineMethod] = {
    "contour": ContourMethod(),
    "hatch": HatchMethod(),
    "flow": FlowMethod(),
}
