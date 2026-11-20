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
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.penplot.svgfont import get_svg_face  # noqa: E402

PAGE_W, PAGE_H, MARGIN, PEN = 210.0, 297.0, 10.0, 0.4

Contour = np.ndarray  # (n, 2) closed polygon, last point != first


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------
def circle(cx: float, cy: float, r: float, n: int = 360) -> Contour:
    t = np.linspace(0, 2 * math.pi, n, endpoint=False)
    return np.column_stack([cx + r * np.cos(t), cy + r * np.sin(t)])


def stroke_text(text: str, x0: float, x1: float, top: float, cap_h: float) -> list[np.ndarray]:
    """Single-stroke ZnikoSL capitals (the shop's plotter font) spanning x0..x1.

    The cap height shrinks if the line would not fit without overlapping. Umlaut
    dots rise above ``top``.
    """
    face = get_svg_face()
    text = text.upper()
    while True:
        k = cap_h / face["cap_height"]
        adv = [(face["glyphs"][c]["advance"] if c in face["glyphs"] else face["space_advance"]) * k for c in text]
        track = ((x1 - x0) - sum(adv)) / max(len(text) - 1, 1)
        if track >= 0.12 * cap_h or cap_h < 4:
            break
        cap_h *= 0.95
    out, x = [], x0
    for c, a in zip(text, adv):
        g = face["glyphs"].get(c)
        if g:
            for line in g["lines"]:
                out.append(np.array([(x + px * k, top + py * k) for px, py in line]))
        x += a + track
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

    def text(self, text: str, x0: float, x1: float, top: float, cap_h: float) -> None:
        for line in stroke_text(text, x0, x1, top, cap_h):
            self.add_polyline(line)

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
    sh.text("BAUHAUS", 148, 820, 68, 70)
    sh.text("Ausstellung", 128, 590, 1215, 47)
    sh.text("1919", 700, 848, 1215, 47)
    return sh.svg()


def poster2() -> str:
    w, h = 1024, 1424
    cx, cy = 508, 666
    sh = Sheet(w, h, "Bauhaus 1919 - concentric circles (outline + hatch)")
    c_out, c_mid, c_in = (circle(cx, cy, r, 720) for r in (593, 435, 269))
    sh.fill([c_out, c_mid], [(45.0, 2.4)])  # light pink  -> sparse
    sh.fill([c_mid, c_in], [(45.0, 1.1), (135.0, 1.1)])  # red -> cross-hatch (darkest)
    sh.fill([c_in], [(135.0, 1.7)])  # orange -> medium
    sh.text("BAUHAUS", 50, 346, 1327, 45)
    sh.text("1919", 838, 970, 1327, 45)
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
    sh.text("BAUHAUS", 78, 405, 102, 38)
    sh.text("1919", 668, 752, 113, 30)
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
    sh.text("BAUHAUS", 12, 308, 18, 39)
    sh.text("1923", 420, 465, 17, 13)
    sh.text("S\u00dcTTERLIN BADENERSTRASSE 109", 12, 468, 685, 20)
    return sh.svg()


def poster5(src: Path) -> str:
    """Dot grid: each dot is detected and redrawn as an exact circle."""
    img = cv2.imread(str(src), cv2.IMREAD_GRAYSCALE)
    h, w = img.shape[:2]
    mask = (img < 110).astype(np.uint8) * 255
    mask[1120:, :] = 0  # lettering
    n, _, stats, cent = cv2.connectedComponentsWithStats(mask)
    dots = [(cent[i], stats[i]) for i in range(1, n) if 600 < stats[i][4] < 4000]
    r = float(np.median([math.sqrt(s[4] / math.pi) for _, s in dots]))
    sh = Sheet(w, h, "Bauhaus 1919 - dot grid (outline + hatch)")
    sh.fill([circle(c[0], c[1], r * 0.97, 72) for c, _ in dots], BLACK)
    sh.add_polyline(np.array([[46, 1182], [836, 1182]], float))
    sh.add_polyline(np.array([[46, 1184], [836, 1184]], float))
    sh.text("BAUHAUS", 48, 430, 1200, 45)
    sh.text("1919", 740, 836, 1210, 35)
    return sh.svg()


def poster6(src: Path) -> str:
    img = cv2.imread(str(src), cv2.IMREAD_GRAYSCALE)
    h, w = img.shape[:2]
    mask = (img < 110).astype(np.uint8) * 255
    keep = np.zeros_like(mask)
    keep[300:1060, :] = 255  # the blobs only
    sh = Sheet(w, h, "Bauhaus Juli-Sept 1923 - blobs (outline + hatch)")
    sh.fill(_smooth_contours(cv2.bitwise_and(mask, keep)), BLACK)
    sh.text("BAUHAUS", 82, 752, 112, 46)
    sh.add_polyline(np.array([[80, 195], [752, 195]], float))
    sh.text("JULI - SEPT 1923", 498, 752, 222, 25)
    return sh.svg()


def main() -> None:
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    (out / "bauhaus-1-bars.svg").write_text(poster1(src / "1.webp"))
    (out / "bauhaus-2-circles.svg").write_text(poster2())
    (out / "bauhaus-3-ribbon.svg").write_text(poster3())
    (out / "bauhaus-4-sun.svg").write_text(poster4(src / "4.jpg"))
    (out / "bauhaus-5-dots.svg").write_text(poster5(src / "5.webp"))
    (out / "bauhaus-6-blobs.svg").write_text(poster6(src / "6.webp"))
    print("wrote", *sorted(p.name for p in out.glob("bauhaus-*.svg")))


if __name__ == "__main__":
    main()
