"""Raster circle detection for wheels, hubs and round holes.

Thinning a 1 px circle gives a polygon whose corners survive smoothing, and
fitting circles to traced strokes fails because rings are cut at the spokes.
Detecting on the raster instead:

1. ``cv2.HoughCircles`` proposes circles;
2. a *ring-support* test keeps only circles whose circumference is inked
   almost all the way round (rejects rounded-rectangle corners and clutter);
3. a seed is trusted if it is clean and big enough, or has a concentric
   partner; each trusted centre is then scanned across radii to complete the
   family (tyre / rim / hub) that Hough alone only half finds.

``mask_circles`` blanks the annulus of every ring so the line tracer does not
also emit its faceted copy; ``circle_polyline`` draws the exact replacement.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from backend.penplot.methods import Polyline

INK = 200            # pixels darker than this count as drawn line
SUPPORT_MIN = 0.80   # fraction of the circumference that must be inked
SEED_CLEAN = 0.95    # a lone seed needs this much support (and r >= SEED_MIN_R)
SEED_MIN_R = 8.0
CENTER_TOL = 3.0     # px: seeds closer than this share a centre
RING_GAP = 2.0       # px: minimum spacing between rings of one family
ANGLES = 90


def ring_support(ink: np.ndarray, cx: float, cy: float, r: float,
                 tol: float = 1.6) -> float:
    """Fraction of ``ANGLES`` directions with ink within ``tol`` px of radius r."""
    a = np.linspace(0, 2 * math.pi, ANGLES, endpoint=False)
    hit = np.zeros(ANGLES, bool)
    h, w = ink.shape
    for dr in np.arange(-tol, tol + 0.01, 0.8):
        x = np.rint(cx + (r + dr) * np.cos(a)).astype(int)
        y = np.rint(cy + (r + dr) * np.sin(a)).astype(int)
        ok = (x >= 0) & (x < w) & (y >= 0) & (y < h)
        hit[ok] |= ink[y[ok], x[ok]]
    return float(hit.mean())


def detect_circles(gray: np.ndarray, *, min_r: float = 4.0,
                   max_r: float = 60.0) -> list[tuple[float, float, float]]:
    """Return ``[(cx, cy, r)]`` in image px for clean, full circles."""
    ink = gray < INK
    found = cv2.HoughCircles(
        cv2.GaussianBlur(gray, (3, 3), 0), cv2.HOUGH_GRADIENT_ALT, dp=1.5,
        minDist=3, param1=200, param2=0.8, minRadius=int(min_r),
        maxRadius=int(max_r))
    seeds = []
    for cx, cy, r in ([] if found is None else found[0]):
        s = ring_support(ink, cx, cy, r)
        if s >= SUPPORT_MIN:
            seeds.append((float(cx), float(cy), float(r), s))

    # Cluster seeds by centre; trust clean-and-large seeds or concentric pairs.
    clusters: list[list[tuple[float, float, float, float]]] = []
    for sd in sorted(seeds, key=lambda t: -t[3]):
        for cl in clusters:
            if math.hypot(sd[0] - cl[0][0], sd[1] - cl[0][1]) <= CENTER_TOL:
                cl.append(sd)
                break
        else:
            clusters.append([sd])
    rings: list[tuple[float, float, float]] = []
    for cl in clusters:
        clean = any(s[3] >= SEED_CLEAN and s[2] >= SEED_MIN_R for s in cl)
        if not (clean or len(cl) >= 2):
            continue
        cx = float(np.mean([s[0] for s in cl]))
        cy = float(np.mean([s[1] for s in cl]))
        rs = np.arange(max(min_r, 3.0), max_r, 0.25)
        # Narrow tolerance so a 1-2 px stroke gives a sharp plateau, then take
        # each above-threshold run's support-weighted centre as the radius.
        sup = np.array([ring_support(ink, cx, cy, r, tol=0.9) for r in rs])
        peaks: list[float] = []
        i = 0
        while i < len(rs):
            if sup[i] < SUPPORT_MIN:
                i += 1
                continue
            j = i
            while j + 1 < len(rs) and sup[j + 1] >= SUPPORT_MIN:
                j += 1
            run = slice(i, j + 1)
            peaks.append(float(np.average(rs[run], weights=sup[run])))
            i = j + 1
        # Adjacent runs closer than RING_GAP are one thick stroke.
        merged: list[float] = []
        for p in peaks:
            if merged and p - merged[-1] < RING_GAP:
                merged[-1] = (merged[-1] + p) / 2
            else:
                merged.append(p)
        peaks = merged
        rings.extend((cx, cy, r) for r in sorted(peaks))
    return rings


def mask_circles(gray: np.ndarray, circles, *, half_width: float = 2.5) -> np.ndarray:
    """Copy of ``gray`` with each ring's annulus painted paper-white."""
    out = gray.copy()
    for cx, cy, r in circles:
        cv2.circle(out, (int(round(cx)), int(round(cy))), int(round(r)), 255,
                   thickness=max(int(round(2 * half_width)), 1))
    return out


def circle_polyline(cx: float, cy: float, r: float, step_deg: float = 3.0) -> Polyline:
    n = max(int(360 / step_deg), 8)
    pts = [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n))
           for k in range(n)]
    return pts + [pts[0]]
