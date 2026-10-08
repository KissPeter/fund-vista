"""Closed-outline tracing method (REF-002 Phase 4).

Pure move from :mod:`backend.penplot.methods` — identical behavior.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import cv2
import numpy as np

if TYPE_CHECKING:
    from backend.penplot.methods import MethodContext, Polyline

log = logging.getLogger(__name__)


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


__all__ = ["ContourMethod"]
