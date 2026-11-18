"""Build the SQLite extract from an OSM PBF (needs ``pip install osmium``).

    python -m backend.osm_local.build hungary-latest.osm.pbf osm-hu.sqlite \\
        --poly hungary.poly

Pass 1 reads the relations (only those citymap selects), pass 2 the ways
with node locations. Without ``--poly`` the coverage is the rectangle of
the data seen, which is wrong along a clipped border: use the Geofabrik
``.poly`` for a country extract.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from backend.osm_local import coverage as cov
from backend.osm_local.selectors import registry, selector_mask
from backend.osm_local.store import Writer

log = logging.getLogger("backend.osm_local")


def build(pbf: Path, out: Path, poly: Path | None = None) -> dict:
    import osmium  # optional dependency, build machine only

    sels = registry()
    started = time.monotonic()

    # Pass 1: relations citymap can select, and the ways they claim.
    relations: list[tuple[int, int, dict, list[tuple[int, str]]]] = []
    member_ids: set[int] = set()
    for rel in osmium.FileProcessor(str(pbf), osmium.osm.RELATION):
        tags = {t.k: t.v for t in rel.tags}
        sel = selector_mask("relation", tags, sels)
        if not sel:
            continue
        members = [(m.ref, m.role) for m in rel.members if m.type == "w"]
        if members:
            relations.append((rel.id, sel, tags, members))
            member_ids.update(ref for ref, _ in members)
    log.info("pass 1: %d relations, %d member ways", len(relations), len(member_ids))

    # Pass 2: ways (with node coordinates).
    writer = Writer(out)
    lat_min = lon_min = 1e9
    lat_max = lon_max = -1e9
    for way in osmium.FileProcessor(str(pbf), osmium.osm.NODE | osmium.osm.WAY).with_locations():
        if not way.is_way():
            continue
        tags = {t.k: t.v for t in way.tags}
        sel = selector_mask("way", tags, sels)
        if not sel and way.id not in member_ids:
            continue
        ids: list[int] = []
        pts: list[tuple[float, float]] = []
        for n in way.nodes:
            if n.location.valid():
                ids.append(n.ref)
                pts.append((n.lat, n.lon))
        if len(ids) < 2:
            continue
        writer.add_way(way.id, sel, tags, ids, pts, keep_bounds=way.id in member_ids)
        for lat, lon in pts:
            lat_min, lat_max = min(lat_min, lat), max(lat_max, lat)
            lon_min, lon_max = min(lon_min, lon), max(lon_max, lon)
    for rid, sel, tags, members in relations:
        writer.add_relation(rid, sel, tags, members)

    rings = cov.load_poly(poly) if poly else cov.rect_ring((lat_min, lon_min, lat_max, lon_max))
    ts = ""
    try:
        ts = osmium.io.Reader(str(pbf)).header().get("osmosis_replication_timestamp") or ""
    except Exception:  # header is best-effort metadata
        pass
    writer.finish(rings, ts, pbf.name)
    stats = {
        "ways": writer.way_count, "relations": writer.rel_count,
        "seconds": round(time.monotonic() - started, 1),
        "bytes": out.stat().st_size,
    }
    log.info("built %s: %s", out, stats)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pbf", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--poly", type=Path, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    print(build(args.pbf, args.out, args.poly))


if __name__ == "__main__":
    main()
