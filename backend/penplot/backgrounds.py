"""Vendored preview backgrounds for the pen-plot display layer.

Two textures, white e-commerce frames already trimmed at vendor time
(see module docstring for the procedure), stored as JPEGs next to this file:

- ``dark-texture`` — charcoal product photo (pairs with white pen lines).
- ``light-texture`` — pale product photo (pairs with red/black/blue pens).

Display-only: the background is embedded in the served SVG as a fill-only
``<image>`` stretched over the full page (``preserveAspectRatio="none"``).
It never enters geometry, stats, or the vpype recipe — plotters only draw
the stroked ``<path>`` layer. Pen color and background are independent
params (any combination is valid).
"""

from __future__ import annotations

import base64
import functools
import os

import numpy as np
from PIL import Image

_BACKGROUNDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backgrounds")

# id -> (filename, label, source URL). Source URLs are provenance only;
# converts never fetch the network — bytes come from the vendored files.
BACKGROUNDS: dict[str, dict[str, str]] = {
    "none": {
        "filename": "",
        "label": "None (plain paper)",
        "source": "",
    },
    "dark-texture": {
        "filename": "dark-texture.jpg",
        "label": "Dark texture (for white pen)",
        "source": "https://www.ilmarket.hu/cdn/shop/files/078231209_0_1448x2006.jpg?v=1789241014",
    },
    "light-texture": {
        "filename": "light-texture.jpg",
        "label": "Light texture (for red / black / blue pen)",
        "source": "https://s13emagst.akamaized.net/products/132357/132356154/images/res_c62ebfeeaa56d62efb5003123f1de3e1.jpg?width=720&height=720&hash=7032669015377C063DF4421280F4AD24",
    },
}

BACKGROUND_IDS = tuple(BACKGROUNDS)

# Display pen colors: name -> SVG stroke value (allowlist — the only
# values to_svg will ever emit, so no injection via params).
LINE_COLORS: dict[str, str] = {
    "black": "#000000",
    "white": "#FFFFFF",
    "red": "#FF0000",
    "blue": "#0000FF",
}

LINE_COLOR_IDS = tuple(LINE_COLORS)


def trim_white_border(gray: np.ndarray, threshold: int = 250) -> np.ndarray:
    """Crop near-white frame rows/columns (e-commerce photo borders).

    ``gray`` is a 2D uint8 array; pixels >= threshold count as frame.
    Returns the bounding box of content pixels, or the input unchanged
    when no content pixel exists (blank image).
    """
    mask = gray < threshold
    if not mask.any():
        return gray
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    return gray[rows[0]: rows[-1] + 1, cols[0]: cols[-1] + 1]


def background_path(background: str) -> str | None:
    entry = BACKGROUNDS.get(background)
    if not entry or not entry["filename"]:
        return None
    return os.path.join(_BACKGROUNDS_DIR, entry["filename"])


@functools.lru_cache(maxsize=8)
def get_background_data_uri(background: str) -> str | None:
    """Return a ``data:image/jpeg;base64,`` URI for a vendored background.

    ``"none"`` (and unknown ids) map to None. Files are already
    white-border-trimmed at vendor time; this path does no image math,
    only base64 — safe to call per convert (result is content-addressed
    and cached on disk anyway).
    """
    path = background_path(background)
    if path is None or not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        raw = fh.read()
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
