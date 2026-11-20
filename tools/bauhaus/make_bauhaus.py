"""Rebuild three Bauhaus 1919 posters as pen-plotter SVGs: outlines + hatching.

Usage (from the repo root):

    backend/.venv/bin/python -I tools/bauhaus/make_bauhaus.py SRC_DIR OUT_DIR

SRC_DIR holds the reference raster ``1.webp`` (stacked bars) and ``4.jpg`` (sun over
waves); their shapes are traced with OpenCV.  Posters 2 and 3 are pure geometry measured off the
references.  Every filled area becomes a closed outline plus parallel hatch
lines (even-odd scanlines, so holes and letter counters work); there are no
backgrounds, no fills and no fonts: single stroke colour, 0.4 mm pen, A4
portrait, 10 mm margin.  Tone is expressed by hatch pitch / direction, because
the plotter has one pen.

Lettering is turned into outlines first (Futura from macOS for 1 and 3, a
hand-built block face for 2), then hatched like any other shape.
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path

import cv2
import numpy as np
from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTCollection

FUTURA = "/System/Library/Fonts/Supplemental/Futura.ttc"
FUTURA_MEDIUM, FUTURA_BOLD = 0, 2
PAGE_W, PAGE_H, MARGIN, PEN = 210.0, 297.0, 10.0, 0.4

Contour = np.ndarray  # (n, 2) closed polygon, last point != first


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------
def circle(cx: float, cy: float, r: float, n: int = 360) -> Contour:
    t = np.linspace(0, 2 * math.pi, n, endpoint=False)
    return np.column_stack([cx + r * np.cos(t), cy + r * np.sin(t)])


class _Flatten(BasePen):
    def __init__(self, gs, k, ox, oy):
        super().__init__(gs)
        self.k, self.ox, self.oy = k, ox, oy
        self.contours: list[list[tuple[float, float]]] = []
        self.cur: list[tuple[float, float]] = []

    def _p(self, p):
        return (self.ox + p[0] * self.k, self.oy - p[1] * self.k)

    def _moveTo(self, p):
        self.cur = [self._p(p)]

    def _lineTo(self, p):
        self.cur.append(self._p(p))

    def _curveToOne(self, a, b, c):
        p0 = np.array(self.cur[-1])
        pa, pb, pc = (np.array(self._p(q)) for q in (a, b, c))
        for t in np.linspace(0, 1, 13)[1:]:
            q = (1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * pa + 3 * (1 - t) * t**2 * pb + t**3 * pc
            self.cur.append((q[0], q[1]))

    def _qCurveToOne(self, a, b):
        p0 = np.array(self.cur[-1])
        pa, pb = np.array(self._p(a)), np.array(self._p(b))
        for t in np.linspace(0, 1, 9)[1:]:
            q = (1 - t) ** 2 * p0 + 2 * (1 - t) * t * pa + t**2 * pb
            self.cur.append((q[0], q[1]))

    def _closePath(self):
        if len(self.cur) > 2:
            self.contours.append(self.cur)
        self.cur = []

    _endPath = _closePath


def text_contours(index: int, text: str, x0: float, x1: float, baseline: float, cap_h: float):
    """Flattened glyph outlines of ``text`` spanning x0..x1 (tracking absorbs the slack)."""
    from fontTools.pens.boundsPen import BoundsPen

    font = TTCollection(FUTURA).fonts[index]
    gs, cmap = font.getGlyphSet(), font.getBestCmap()
    k = cap_h / (font["OS/2"].sCapHeight or font["head"].unitsPerEm * 0.7)
    names = [cmap[ord(c)] for c in text]
    adv = [font["hmtx"][n][0] * k for n in names]

    def bounds(n):
        bp = BoundsPen(gs)
        gs[n].draw(bp)
        return bp.bounds

    lsb = bounds(names[0])[0] * k
    ink = sum(adv[:-1]) + bounds(names[-1])[2] * k - lsb
    track = ((x1 - x0) - ink) / max(len(text) - 1, 1)
    x, out = x0 - lsb, []
    for n, a in zip(names, adv):
        pen = _Flatten(gs, k, x, baseline)
        gs[n].draw(pen)
        out += [np.array(c) for c in pen.contours]
        x += a + track
    return out


BLOCK = {  # stroke paths on a 4 x 6 grid
    "B": [[(0, 0), (3.4, 0), (3.4, 3), (0, 3)], [(0, 3), (4, 3), (4, 6), (0, 6)], [(0, 0), (0, 6)]],
    "A": [[(0, 6), (0, 0), (4, 0), (4, 6)], [(0, 3), (4, 3)]],
    "U": [[(0, 0), (0, 6), (4, 6), (4, 0)]],
    "H": [[(0, 0), (0, 6)], [(4, 0), (4, 6)], [(0, 3), (4, 3)]],
    "S": [[(4, 0), (0, 0), (0, 3), (4, 3), (4, 6), (0, 6)]],
    "1": [[(0, 1.2), (2, 0), (2, 6)], [(0, 6), (4, 6)]],
    "9": [[(4, 3), (0, 3), (0, 0), (4, 0), (4, 6), (0, 6)]],
    "0": [[(0, 0), (4, 0), (4, 6), (0, 6), (0, 0)]],
    "2": [[(0, 0), (4, 0), (4, 3), (0, 3), (0, 6), (4, 6)]],
    "3": [[(0, 0), (4, 0), (4, 6), (0, 6)], [(1, 3), (4, 3)]],
    "T": [[(0, 0), (4, 0)], [(2, 0), (2, 6)]],
    "E": [[(4, 0), (0, 0), (0, 6), (4, 6)], [(0, 3), (3, 3)]],
    "R": [[(0, 6), (0, 0), (4, 0), (4, 3), (0, 3)], [(2, 3), (4, 6)]],
    "L": [[(0, 0), (0, 6), (4, 6)]],
    "I": [[(0, 0), (4, 0)], [(2, 0), (2, 6)], [(0, 6), (4, 6)]],
    "N": [[(0, 6), (0, 0), (4, 0), (4, 6)]],
    "D": [[(0, 0), (3, 0), (4, 1), (4, 5), (3, 6), (0, 6), (0, 0)]],
    "\u00dc": [[(0, 0), (0, 6), (4, 6), (4, 0)], [(0.5, -2), (0.5, -1)], [(3.5, -2), (3.5, -1)]],
}


def block_contours(text: str, x: float, y: float, cell: float, pitch: float) -> list[Contour]:
    """Union of the stroked block glyphs as rectilinear polygons (via a hi-res raster)."""
    ss = 16  # raster px per cell
    out: list[Contour] = []
    top = 3  # grid units of head-room for umlaut dots
    for i, ch in enumerate(text):
        if ch == " ":
            continue
        w, h = int((4 + 1.5) * ss), int((top + 6 + 1.5) * ss)
        img = np.zeros((h, w), np.uint8)
        for stroke in BLOCK[ch]:
            pts = np.array([[(px + 0.5) * ss, (py + top + 0.5) * ss] for px, py in stroke], np.float32)
            for a, b in zip(pts[:-1], pts[1:]):
                d = b - a
                n = d / (np.linalg.norm(d) or 1)
                nrm = np.array([-n[1], n[0]]) * ss / 2
                quad = np.array([a - nrm, a + nrm, b + nrm, b - nrm], np.int32)  # butt ends
                cv2.fillConvexPoly(img, quad, 255)
            for p in pts[1:-1]:
                cv2.rectangle(img, (int(p[0] - ss / 2), int(p[1] - ss / 2)), (int(p[0] + ss / 2), int(p[1] + ss / 2)), 255, -1)
        cnts, hier = cv2.findContours(img, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            a = cv2.approxPolyDP(c, 0.8, True).reshape(-1, 2).astype(float)
            out.append(np.column_stack([x + i * pitch + a[:, 0] * cell / ss, y + a[:, 1] * cell / ss - top * cell]))
    return out


# --------------------------------------------------------------------------
# drawing model: strokes only
# --------------------------------------------------------------------------
class Sheet:
    """Collects polylines in source-pixel space, writes A4 mm SVG."""

    def __init__(self, w: float, h: float, title: str):
        self.w, self.h, self.title = w, h, title
        self.s = min((PAGE_W - 2 * MARGIN) / w, (PAGE_H - 2 * MARGIN) / h)  # mm per px
        self.ox, self.oy = (PAGE_W - w * self.s) / 2, (PAGE_H - h * self.s) / 2
        self.lines: list[np.ndarray] = []
        self.clip = (0.0, 0.0, w, h)

    # -- clipping ----------------------------------------------------------
    def _clip_seg(self, a, b):
        x0, y0, x1, y1 = self.clip
        dx, dy = b[0] - a[0], b[1] - a[1]
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
            if p == 0:
                if q < 0:
                    return None
                continue
            t = q / p
            if p < 0:
                if t > t1:
                    return None
                t0 = max(t0, t)
            else:
                if t < t0:
                    return None
                t1 = min(t1, t)
        return (a[0] + t0 * dx, a[1] + t0 * dy), (a[0] + t1 * dx, a[1] + t1 * dy)

    def add_polyline(self, pts: np.ndarray, closed: bool = False) -> None:
        pts = np.asarray(pts, float)
        if closed:
            pts = np.vstack([pts, pts[:1]])
        run: list = []
        for a, b in zip(pts[:-1], pts[1:]):
            seg = self._clip_seg(a, b)
            if seg is None:
                if len(run) > 1:
                    self.lines.append(np.array(run))
                run = []
                continue
            if run and np.allclose(run[-1], seg[0]):
                run.append(seg[1])
            else:
                if len(run) > 1:
                    self.lines.append(np.array(run))
                run = [seg[0], seg[1]]
        if len(run) > 1:
            self.lines.append(np.array(run))

    # -- hatching ----------------------------------------------------------
    def hatch(self, contours: list[Contour], angle_deg: float, pitch_mm: float) -> None:
        """Even-odd parallel hatch over all ``contours`` taken together."""
        pitch = pitch_mm / self.s
        th = math.radians(angle_deg)
        c, s_ = math.cos(th), math.sin(th)
        rot = np.array([[c, s_], [-s_, c]])  # world -> hatch frame (hatch lines are horizontal)
        polys = [np.asarray(p, float) @ rot.T for p in contours]
        edges = np.vstack([np.column_stack([p, np.roll(p, -1, axis=0)]) for p in polys])
        ex0, ey0, ex1, ey1 = edges.T
        ylo, yhi = min(p[:, 1].min() for p in polys), max(p[:, 1].max() for p in polys)
        y = ylo + pitch / 2
        while y < yhi:
            m = (ey0 <= y) != (ey1 <= y)
            if m.any():
                t = (y - ey0[m]) / (ey1[m] - ey0[m])
                xs = np.sort(ex0[m] + t * (ex1[m] - ex0[m]))
                for xa, xb in zip(xs[0::2], xs[1::2]):
                    if xb - xa > 0.05:
                        seg = np.array([[xa, y], [xb, y]]) @ rot  # back to world
                        self.add_polyline(seg)
            y += pitch

    def fill(self, contours: list[Contour], hatches: list[tuple[float, float]], outline: bool = True) -> None:
        if outline:
            for p in contours:
                self.add_polyline(p, closed=True)
        for angle, pitch in hatches:
            self.hatch(contours, angle, pitch)

    # -- output ------------------------------------------------------------
    def svg(self) -> str:
        paths = []
        for ln in self.lines:
            xy = (ln * self.s) + [self.ox, self.oy]
            paths.append("M" + " L".join(f"{x:.2f} {y:.2f}" for x, y in xy))
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{PAGE_W:g}mm" height="{PAGE_H:g}mm" '
            f'viewBox="0 0 {PAGE_W:g} {PAGE_H:g}">\n<title>{self.title}</title>\n'
            f'<g fill="none" stroke="#000000" stroke-width="{PEN}" stroke-linecap="round" stroke-linejoin="round">\n'
            f'<path d="{" ".join(paths)}"/>\n</g>\n</svg>\n'
        )


BLACK = [(0.0, 0.6)]  # near-solid: 0.4 mm pen at 0.6 mm pitch


# --------------------------------------------------------------------------
# posters
# --------------------------------------------------------------------------
def poster1(src: Path) -> str:
    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = (gray < 110).astype(np.uint8) * 255
    keep = np.zeros_like(mask)
    keep[190:1170, 60:910] = 255  # artwork only: no title, no wooden frame
    cnts, _ = cv2.findContours(cv2.bitwise_and(mask, keep), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    sh = Sheet(w, h, "Bauhaus Ausstellung 1919 - stacked bars (outline + hatch)")
    for c in cnts:
        if cv2.contourArea(c) < 40:
            continue
        a = cv2.approxPolyDP(c, 1.6, True).reshape(-1, 2).astype(float)
        sh.fill([a], BLACK)
    sh.fill(text_contours(FUTURA_MEDIUM, "BAUHAUS", 148, 820, 138, 70), BLACK)
    sh.fill(text_contours(FUTURA_MEDIUM, "Ausstellung", 128, 590, 1262, 47), BLACK)
    sh.fill(text_contours(FUTURA_MEDIUM, "1919", 700, 848, 1262, 47), BLACK)
    return sh.svg()


def poster2() -> str:
    w, h = 1024, 1424
    cx, cy = 508, 666
    sh = Sheet(w, h, "Bauhaus 1919 - concentric circles (outline + hatch)")
    c_out, c_mid, c_in = (circle(cx, cy, r, 720) for r in (593, 435, 269))
    sh.fill([c_out, c_mid], [(45.0, 2.4)])  # light pink  -> sparse
    sh.fill([c_mid, c_in], [(45.0, 1.1), (135.0, 1.1)])  # red -> cross-hatch (darkest)
    sh.fill([c_in], [(135.0, 1.7)])  # orange -> medium
    cell = 7.2
    sh.fill(block_contours("BAUHAUS", 50, 1327, cell, 44.5), BLACK)
    sh.fill(block_contours("1919", 838, 1327, cell, 34.5), BLACK)
    return sh.svg()


def poster3() -> str:
    w, h = 832, 1210
    n, sp, r_in = 17, 10.25, 19.0
    c1, c2x, c3, y2 = (455.0, 443.0), 310.0, (542.0, 847.0), 645.0
    sh = Sheet(w, h, "Bauhaus 1919 - S ribbon (outline + hatch)")

    def arc(cx, cy, r, a0, a1):
        t = np.linspace(math.radians(a0), math.radians(a1), 90)
        return np.column_stack([cx + r * np.cos(t), cy + r * np.sin(t)])

    for j in range(n):
        r1, r2 = r_in + sp * (n - 1 - j), r_in + sp * j
        pts = np.vstack(
            [
                [(308, c1[1] - r1)],
                arc(c1[0], c1[1], r1, -90, 90),  # right turn, clockwise
                arc(c2x, y2, r2, -90, -270),  # left turn, counter-clockwise
                arc(c3[0], c3[1], r1, -90, 90),
                [(400, c3[1] + r1)],
            ]
        )
        sh.add_polyline(pts)
    sh.fill([circle(204, 339, 82)], BLACK)
    sh.fill([circle(287, 951, 82)], BLACK)
    sh.fill(text_contours(FUTURA_BOLD, "BAUHAUS", 78, 405, 140, 38), BLACK)
    sh.fill(text_contours(FUTURA_BOLD, "1919", 668, 752, 143, 30), BLACK)
    sh.add_polyline(np.array([[78, 170], [752, 170]], float))
    sh.add_polyline(np.array([[78, 172], [752, 172]], float))
    return sh.svg()


def _smooth_contours(mask: np.ndarray, pad: int = 8, up: int = 4, eps: float = 1.3) -> list[Contour]:
    """Smooth outlines of a binary mask, in source px; edge padding keeps page-edge cuts open."""
    m = cv2.copyMakeBorder(mask, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
    m = cv2.resize(m, None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC)
    m = cv2.GaussianBlur(m, (0, 0), up * 0.8)
    m = (m > 127).astype(np.uint8) * 255
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    out = []
    for c in cnts:
        if cv2.contourArea(c) < 30 * up * up:
            continue
        a = cv2.approxPolyDP(c, eps, True).reshape(-1, 2).astype(float) / up - pad
        out.append(a)
    return out


def poster4(src: Path) -> str:
    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[..., 0].astype(int), hsv[..., 1], hsv[..., 2]
    yellow = ((hue > 12) & (hue < 28) & (sat > 110) & (val > 150)).astype(np.uint8) * 255
    red = (((hue < 8) | (hue > 170)) & (sat > 110) & (val > 120)).astype(np.uint8) * 255
    sh = Sheet(w, h, "Bauhaus 1923 Sutterlin - sun over waves (outline + hatch)")
    sh.fill(_smooth_contours(red), [(45.0, 1.0), (135.0, 1.0)])  # sun: darkest
    sh.fill(_smooth_contours(yellow), [(45.0, 1.5)])  # waves: medium
    sh.fill(block_contours("BAUHAUS", 12, 18, 5.57, 44.7), BLACK)
    sh.fill(block_contours("1923", 420, 17, 1.86, 11.9), BLACK)
    sh.fill(block_contours("S\u00dcTTERLIN BADENERSTRASSE 109", 12, 685, 2.86, 16.36), BLACK)
    return sh.svg()


def main() -> None:
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    (out / "bauhaus-1-bars.svg").write_text(poster1(src / "1.webp"))
    (out / "bauhaus-2-circles.svg").write_text(poster2())
    (out / "bauhaus-3-ribbon.svg").write_text(poster3())
    (out / "bauhaus-4-sun.svg").write_text(poster4(src / "4.jpg"))
    print("wrote", *sorted(p.name for p in out.glob("bauhaus-*.svg")))


if __name__ == "__main__":
    main()
