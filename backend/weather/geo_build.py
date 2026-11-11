"""Build the vendored Natural Earth geography for the weather map (dev script).

Not used at runtime. Reads the Natural Earth 1:50m GeoJSON files (public
domain) downloaded from the nvkelso/natural-earth-vector repository, simplifies
every line with Ramer-Douglas-Peucker at ``EPS_DEG`` and writes compact JSON
into ``backend/weather/data/``.

Usage (from the worktree root)::

    python -m backend.weather.geo_build <dir-with-the-two-geojson-files>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from backend.weather.geo import simplify

#: Douglas-Peucker tolerance in degrees (plan section 4: about 0.05 degree).
EPS_DEG = 0.05

SOURCES = {
    "coast": "ne_50m_coastline.geojson",
    "borders": "ne_50m_admin_0_boundary_lines_land.geojson",
}

DATA_DIR = Path(__file__).resolve().parent / "data"


def _lines(doc: dict) -> list[list[tuple[float, float]]]:
    out: list[list[tuple[float, float]]] = []
    for feat in doc["features"]:
        geom = feat["geometry"]
        if geom is None:
            continue
        if geom["type"] == "LineString":
            parts = [geom["coordinates"]]
        elif geom["type"] == "MultiLineString":
            parts = geom["coordinates"]
        else:
            continue
        for part in parts:
            pts = [(float(x), float(y)) for x, y, *_ in part]
            if len(pts) >= 2:
                out.append(pts)
    return out


def build(src_dir: Path, out_dir: Path = DATA_DIR) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for key, fname in SOURCES.items():
        doc = json.loads((src_dir / fname).read_text(encoding="utf-8"))
        lines = []
        for pts in _lines(doc):
            simple = simplify(pts, EPS_DEG)
            if len(simple) >= 2:
                lines.append([[round(x, 3), round(y, 3)] for x, y in simple])
        payload = {
            "source": fname,
            "dataset": "Natural Earth 1:50m (public domain)",
            "simplify_deg": EPS_DEG,
            "lines": lines,
        }
        (out_dir / f"{key}.json").write_text(
            json.dumps(payload, separators=(",", ":")), encoding="utf-8"
        )
        counts[key] = len(lines)
    return counts


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m backend.weather.geo_build <src-dir>")
    print(build(Path(sys.argv[1])))
