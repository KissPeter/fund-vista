"""Vendored Natural Earth coast and land-border lines for the weather map.

The JSON files in ``backend/weather/data/`` are written by
:mod:`backend.weather.geo_build` (public domain, Natural Earth 1:50m, simplified
at 0.05 degree). Tests inject a :class:`Geography` built from a tiny fixture,
so no test reads the vendored files or the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from backend.weather.config import Region

DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class Geography:
    """Polylines as ``(n, 2)`` float arrays of ``(lon, lat)`` degrees."""

    coast: list[np.ndarray]
    borders: list[np.ndarray]

    def clipped(self, region: Region, margin_deg: float = 1.0) -> "Geography":
        """Keep the lines whose bounding box meets the region box (plus margin)."""

        def keep(line: np.ndarray) -> bool:
            lon, lat = line[:, 0], line[:, 1]
            return bool(
                lon.max() >= region.lon0 - margin_deg and lon.min() <= region.lon1 + margin_deg
                and lat.max() >= region.lat0 - margin_deg and lat.min() <= region.lat1 + margin_deg
            )

        return Geography(
            coast=[ln for ln in self.coast if keep(ln)],
            borders=[ln for ln in self.borders if keep(ln)],
        )


def _load_layer(path: Path) -> list[np.ndarray]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [np.asarray(line, dtype=float) for line in payload["lines"]]


@lru_cache(maxsize=1)
def load_geography(data_dir: Path = DATA_DIR) -> Geography:
    """The vendored Natural Earth lines (read once per process)."""
    return Geography(
        coast=_load_layer(data_dir / "coast.json"),
        borders=_load_layer(data_dir / "borders.json"),
    )
