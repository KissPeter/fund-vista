"""Generate the blueprint-style stroke SVG fixtures (issue #144).

Two top-level groups, ``<g id="map">`` (roads) and ``<g id="buildings">``
(closed wall rings plus open lines), all ``fill="none"``. Deterministic: the
same arguments always write the same bytes.

Usage: python backend/tools/blueprint_fixtures.py OUT_DIR
"""
from __future__ import annotations

import math
import pathlib
import random
import sys

#: Target sizes (bytes) of the three benchmark fixtures.
TARGETS = {"small": 200_000, "typical": 2_000_000, "worst": 9_000_000}


def _ring(rng: random.Random, cx: float, cy: float) -> str:
    w = rng.uniform(2.0, 9.0)
    h = rng.uniform(2.0, 9.0)
    pts = [(cx, cy), (cx + w, cy), (cx + w, cy + h), (cx, cy + h)]
    d = "M " + " L ".join(f"{x:.2f} {y:.2f}" for x, y in pts) + " Z"
    return f'<path d="{d}"/>'


def _road(rng: random.Random, x: float, y: float) -> str:
    a = rng.uniform(0, math.tau)
    pts = [(x + math.cos(a) * k * 1.5, y + math.sin(a) * k * 1.5) for k in range(6)]
    d = "M " + " L ".join(f"{px:.2f} {py:.2f}" for px, py in pts)
    return f'<path d="{d}"/>'


def build(target_bytes: int, seed: int = 144) -> str:
    rng = random.Random(seed)
    head = ('<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" '
            'viewBox="0 0 210 297">\n')
    map_parts: list[str] = []
    building_parts: list[str] = []
    size = len(head) + len('<g id="map" fill="none">\n</g>\n<g id="buildings" fill="none">\n</g>\n</svg>\n')
    while size < target_bytes:
        x = rng.uniform(10, 200)
        y = rng.uniform(10, 287)
        if rng.random() < 0.25:
            piece = _road(rng, x, y)
            map_parts.append(piece)
        else:
            piece = _ring(rng, x, y)
            building_parts.append(piece)
        size += len(piece) + 1
    return (
        head
        + '<g id="map" fill="none">\n' + "\n".join(map_parts) + "\n</g>\n"
        + '<g id="buildings" fill="none">\n' + "\n".join(building_parts) + "\n</g>\n"
        + "</svg>\n"
    )


def main(argv: list[str]) -> int:
    out = pathlib.Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for name, target in TARGETS.items():
        (out / f"blueprint-{name}.svg").write_text(build(target), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
