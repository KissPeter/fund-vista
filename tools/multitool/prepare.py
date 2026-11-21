"""Pre/post steps for the two multitool drawings (see docs in the shop repo: docs/tools-sources.md).

    python tools/multitool/prepare.py sog SRC.png OUT.png          # callouts + logos removed
    python tools/multitool/prepare.py leatherman-src SRC.webp OUT.png  # upright, caption/suffix letters masked
    python tools/multitool/prepare.py leatherman-svg IN.svg OUT.svg    # re-adds the 'a' of 12a / 37a

img2plot is run in between (README of the shop PR lists the exact flags).  Sources are only read.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.penplot.svgfont import get_svg_face  # noqa: E402

# Leatherman: working-copy geometry (rotated source is 2000 x 1096 px)
LM_WHITEOUT = [(840, 1020, 975, 1096), (1633, 190, 1654, 220), (1938, 258, 1960, 288)]  # FIG. 1, a of 12a, a of 37a
LM_SUFFIX_A = [(1636, 194, 1650, 216), (1941, 261, 1955, 283)]  # source boxes of the two suffix letters
# SOG: boxes (920 x 759 source) holding brand marks printed on the tool
SOG_LOGO_BOXES = [(455, 140, 510, 212), (535, 360, 600, 445), (578, 432, 655, 548)]


def sog(src: Path, out: Path) -> None:
    g = cv2.imread(str(src), cv2.IMREAD_GRAYSCALE)  # sources have no alpha that matters (flattened on white)
    core = (g < 70).astype(np.uint8)
    keep = cv2.dilate(core, np.ones((5, 5), np.uint8)).astype(bool)  # grey callouts are not near true black
    g = np.where(keep, g, 255).astype(np.uint8)
    ink = (g < 215).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    for x0, y0, x1, y1 in SOG_LOGO_BOXES:  # drop the small isolated letter shapes inside each logo box
        for i in range(1, n):
            x, y, w, h, area = st[i]
            if x >= x0 and y >= y0 and x + w <= x1 and y + h <= y1 and area < 8000:
                g[lab == i] = 255
    cv2.imwrite(str(out), g)


def leatherman_src(src: Path, out: Path) -> None:
    from PIL import Image, ImageDraw

    im = Image.open(src).convert("L").rotate(-90, expand=True)  # the figure lies on its side
    d = ImageDraw.Draw(im)
    for box in LM_WHITEOUT:
        d.rectangle(box, fill=255)
    im.save(out)


def leatherman_svg(src: Path, out: Path) -> None:
    text = src.read_text()
    pts = np.array([(float(a), float(b)) for a, b in re.findall(r"[ML] ([\d.]+) ([\d.]+)", text)])
    scale = (pts[:, 0].max() - pts[:, 0].min()) / (1984 - 15)
    ox = pts[:, 0].min() - 15 * scale
    oy = pts[:, 1].min() - 15 * scale
    glyph = get_svg_face()["glyphs"]["a"]["lines"]
    gx = [x for line in glyph for x, _ in line]
    gy = [y for line in glyph for _, y in line]
    paths = []
    for x0, y0, x1, y1 in LM_SUFFIX_A:
        k = (y1 - y0) / (max(gy) - min(gy))
        for line in glyph:
            xy = [(ox + (x0 + (x - min(gx)) * k) * scale, oy + (y0 + (y - min(gy)) * k) * scale) for x, y in line]
            paths.append("M " + " L ".join(f"{x:.3f} {y:.3f}" for x, y in xy))
    extra = "".join(f'<path d="{p}"/>\n' for p in paths)
    out.write_text(text.replace("</g></svg>", extra + "</g></svg>"))


if __name__ == "__main__":
    cmd, a, b = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    {"sog": sog, "leatherman-src": leatherman_src, "leatherman-svg": leatherman_svg}[cmd](a, b)
