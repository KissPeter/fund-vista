"""SQLite store: schema, bulk writer and Overpass-shaped reader.

Ways keep their node ids and coordinates inline (packed ``int64`` ids and
``int32`` lat/lon pairs in 1e-7 degrees), so there is no node table: a
Hungary extract stays around 1-2 GB. Ways and relations carry a ``sel``
bitmask of the registry selectors they satisfy; spatial lookup goes through
an R*Tree. The blobs use native byte order - build and serve on one machine
(``meta.byteorder`` is checked on open).
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import threading
from array import array
from pathlib import Path
from typing import Iterable

from backend.citymap.shortways import drop_short_ways, polyline_length_m
from backend.osm_local import coverage as cov
from backend.osm_local.ql import AroundMember, BBox, Member
from backend.osm_local.selectors import AROUND_FILTERS, Selector, registry, tags_match

SCHEMA_VERSION = "2"
_READABLE_SCHEMAS = ("1", "2")  # 1 has no ways.len_m: filtered after the query
_E7 = 10_000_000
_CHUNK = 500
_M_PER_DEG_LAT = 110_540.0  # the constants backend.airports uses for its own metres
_M_PER_DEG_LON = 111_320.0


def around_box(lat: float, lon: float, radius_m: float) -> BBox:
    """South, west, north, east of the square around a circle (a hair generous)."""
    dlat = radius_m / _M_PER_DEG_LAT * 1.01
    dlon = radius_m / (_M_PER_DEG_LON * max(0.01, math.cos(math.radians(lat)))) * 1.01
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def _within(flat, lat0: float, lon0: float, radius_m: float) -> bool:
    """Does the polyline ``flat`` (lat, lon pairs in 1e-7 degrees) come within ``radius_m`` of the point?"""
    kx = _M_PER_DEG_LON * math.cos(math.radians(lat0)) / _E7
    ky = _M_PER_DEG_LAT / _E7
    cx, cy = lon0 * _E7, lat0 * _E7
    r2 = radius_m * radius_m
    px = (flat[1] - cx) * kx
    py = (flat[0] - cy) * ky
    for i in range(2, len(flat), 2):
        qx = (flat[i + 1] - cx) * kx
        qy = (flat[i] - cy) * ky
        dx, dy = qx - px, qy - py
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(0.0, min(1.0, -(px * dx + py * dy) / seg))
        ex, ey = px + t * dx, py + t * dy
        if ex * ex + ey * ey <= r2:
            return True
        px, py = qx, qy
    return len(flat) == 2 and px * px + py * py <= r2


def _geometry(flat) -> list[dict]:
    return [{"lat": flat[i] / _E7, "lon": flat[i + 1] / _E7} for i in range(0, len(flat), 2)]

_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE ways (
    id INTEGER PRIMARY KEY, sel INTEGER NOT NULL, tags TEXT NOT NULL,
    nodes BLOB NOT NULL, coords BLOB NOT NULL, len_m REAL NOT NULL);
CREATE VIRTUAL TABLE way_idx USING rtree(id, min_lat, max_lat, min_lon, max_lon);
CREATE TABLE relations (
    id INTEGER PRIMARY KEY, sel INTEGER NOT NULL, tags TEXT NOT NULL,
    members TEXT NOT NULL);
CREATE VIRTUAL TABLE rel_idx USING rtree(id, min_lat, max_lat, min_lon, max_lon);
"""


class StoreError(Exception):
    pass


def _pack(typecode: str, values: Iterable[int]) -> bytes:
    return array(typecode, values).tobytes()


def _unpack(typecode: str, blob: bytes) -> array:
    a = array(typecode)
    a.frombytes(blob)
    return a


class Writer:
    """Bulk loader. Feed it plain data; ``finish`` seals the file atomically."""

    def __init__(self, path: Path) -> None:
        self._final = Path(path)
        self._tmp = self._final.with_name(self._final.name + ".tmp")
        self._tmp.unlink(missing_ok=True)
        self._db = sqlite3.connect(self._tmp)
        self._db.execute("PRAGMA journal_mode=OFF")
        self._db.execute("PRAGMA synchronous=OFF")
        self._db.executescript(_DDL)
        self._ways: list[tuple] = []
        self._way_idx: list[tuple] = []
        self.way_count = 0
        self.rel_count = 0
        self._bounds: dict[int, BBox] = {}  # member way id -> bbox (lat/lon)
        self.sels = registry()

    def add_way(
        self, wid: int, sel: int, tags: dict[str, str],
        node_ids: list[int], latlon: list[tuple[float, float]],
        keep_bounds: bool = False,
    ) -> None:
        if len(node_ids) < 2 or len(node_ids) != len(latlon):
            return
        lats = [p[0] for p in latlon]
        lons = [p[1] for p in latlon]
        box = (min(lats), min(lons), max(lats), max(lons))
        flat = [round(v * _E7) for p in latlon for v in p]
        self._ways.append((
            wid, sel, json.dumps(tags, separators=(",", ":"), ensure_ascii=False),
            _pack("q", node_ids), _pack("i", flat), polyline_length_m(latlon),
        ))
        self._way_idx.append((wid, box[0], box[2], box[1], box[3]))
        if keep_bounds:
            self._bounds[wid] = (box[0], box[1], box[2], box[3])
        self.way_count += 1
        if len(self._ways) >= 20_000:
            self._flush()

    def _flush(self) -> None:
        self._db.executemany("INSERT INTO ways VALUES (?,?,?,?,?,?)", self._ways)
        self._db.executemany("INSERT INTO way_idx VALUES (?,?,?,?,?)", self._way_idx)
        self._ways.clear()
        self._way_idx.clear()

    def add_relation(
        self, rid: int, sel: int, tags: dict[str, str], members: list[tuple[int, str]],
    ) -> None:
        """Relation over way ``members`` ``[(ref, role)]``; needs those ways added with ``keep_bounds``."""
        boxes = [self._bounds[ref] for ref, _ in members if ref in self._bounds]
        if not boxes:
            return
        self._db.execute(
            "INSERT INTO relations VALUES (?,?,?,?)",
            (rid, sel, json.dumps(tags, separators=(",", ":"), ensure_ascii=False),
             json.dumps(members, separators=(",", ":"))),
        )
        self._db.execute(
            "INSERT INTO rel_idx VALUES (?,?,?,?,?)",
            (rid, min(b[0] for b in boxes), max(b[2] for b in boxes),
             min(b[1] for b in boxes), max(b[3] for b in boxes)),
        )
        self.rel_count += 1

    def finish(self, rings: list[dict], osm_timestamp: str, source: str) -> None:
        self._flush()
        meta = {
            "schema": SCHEMA_VERSION,
            "byteorder": sys.byteorder,
            "selectors": json.dumps(self.sels),
            "coverage": json.dumps(rings),
            "osm_timestamp": osm_timestamp,
            "source": source,
            "ways": str(self.way_count),
            "relations": str(self.rel_count),
        }
        self._db.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
        self._db.commit()
        self._db.execute("ANALYZE")
        self._db.close()
        self._tmp.replace(self._final)


class Store:
    """Read-only access; one sqlite connection per thread."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise StoreError(f"no database at {self.path}")
        self._local = threading.local()
        meta = dict(self._conn().execute("SELECT key, value FROM meta"))
        if meta.get("schema") not in _READABLE_SCHEMAS:
            raise StoreError(f"unsupported schema {meta.get('schema')!r}")
        if meta.get("byteorder") != sys.byteorder:
            raise StoreError("database was built on a machine with another byte order")
        self.meta = meta
        self.has_len = meta.get("schema") == "2"
        self.rings: list[dict] = json.loads(meta["coverage"])
        self.sels: list[Selector] = [tuple(s) for s in json.loads(meta["selectors"])]  # type: ignore[misc]
        self._bit = {s: i for i, s in enumerate(self.sels)}
        #: False when ``layers.py`` changed an existing selector: rebuild needed.
        #: Selectors appended since the build only make the new ones unsupported.
        want = registry()
        self.fresh = want[: len(self.sels)] == self.sels

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            # Bigger page cache helps warm reads through the container bind
            # mount. NO mmap_size: mmap over the Colima/virtiofs bind mount
            # killed an uvicorn worker silently (no traceback, no OOM).
            conn.execute("PRAGMA cache_size=-131072")  # 128 MB per thread
            conn.execute("PRAGMA mmap_size=0")
            self._local.conn = conn
        return conn

    def covers(self, bbox: BBox) -> bool:
        return cov.covers(self.rings, bbox)

    def supports(self, member: Member) -> bool:
        return (member.kind, member.filter) in self._bit

    def supports_around(self, member: AroundMember) -> bool:
        srcs = AROUND_FILTERS.get((member.kind, member.filter))
        return bool(srcs) and all(src in self._bit for src in srcs)

    def run_around(self, members: list[AroundMember]) -> dict:
        """Overpass ``out geom`` JSON for the union of ``around`` members."""
        db = self._conn()
        ways: dict[int, tuple[dict, array]] = {}
        rels: dict[int, tuple[dict, list]] = {}
        for m in members:
            mask = 0
            for src in AROUND_FILTERS[(m.kind, m.filter)]:
                mask |= 1 << self._bit[src]
            south, west, north, east = around_box(m.lat, m.lon, m.radius_m)
            args = (north, south, east, west, mask)
            if m.kind == "way":
                for wid, tags_json, coords in db.execute(
                    "SELECT w.id, w.tags, w.coords FROM way_idx i JOIN ways w ON w.id = i.id "
                    "WHERE i.min_lat <= ? AND i.max_lat >= ? AND i.min_lon <= ? "
                    "AND i.max_lon >= ? AND (w.sel & ?) != 0", args,
                ):
                    if wid in ways:
                        continue
                    tags = json.loads(tags_json)
                    flat = _unpack("i", coords)
                    if tags_match(m.filter, tags) and _within(flat, m.lat, m.lon, m.radius_m):
                        ways[wid] = (tags, flat)
            else:
                for rid, tags_json, members_json in db.execute(
                    "SELECT r.id, r.tags, r.members FROM rel_idx i JOIN relations r ON r.id = i.id "
                    "WHERE i.min_lat <= ? AND i.max_lat >= ? AND i.min_lon <= ? "
                    "AND i.max_lon >= ? AND (r.sel & ?) != 0", args,
                ):
                    tags = json.loads(tags_json)
                    if rid not in rels and tags_match(m.filter, tags):
                        # keep the circle with the relation: checked once members are loaded
                        rels[rid] = (tags, [m.lat, m.lon, m.radius_m, json.loads(members_json)])
        need = sorted({ref for _, (_c1, _c2, _c3, mem) in rels.values() for ref, _ in mem})
        geoms: dict[int, array] = {}
        for i in range(0, len(need), _CHUNK):
            part = need[i : i + _CHUNK]
            marks = ",".join("?" * len(part))
            for wid, coords in db.execute(f"SELECT id, coords FROM ways WHERE id IN ({marks})", part):
                geoms[wid] = _unpack("i", coords)
        elements: list[dict] = []
        for wid, (tags, flat) in ways.items():
            elements.append({"type": "way", "id": wid, "tags": tags, "geometry": _geometry(flat)})
        for rid, (tags, (lat, lon, radius, mem)) in rels.items():
            if not any(ref in geoms and _within(geoms[ref], lat, lon, radius) for ref, _ in mem):
                continue
            elements.append({
                "type": "relation", "id": rid, "tags": tags,
                "members": [
                    {"type": "way", "ref": ref, "role": role, "geometry": _geometry(geoms[ref])}
                    for ref, role in mem if ref in geoms
                ],
            })
        return {
            "version": 0.6,
            "generator": "fund-vista-osm-local",
            "osm3s": {"timestamp_osm_base": self.meta.get("osm_timestamp", "")},
            "elements": elements,
        }

    def run(
        self, members: list[Member], min_len_m: float = 0.0, compact: bool = False,
    ) -> dict:
        """Overpass JSON for the union of ``members`` (``out body; >; out skel``).

        ``min_len_m`` > 0 leaves out ways shorter than that (in SQL when the
        database stores lengths, else after the query), so they are never
        read, encoded or held. Relations are still returned.

        ``compact=True`` returns citymap's compact elements (ways carry their
        geometry as ``g = [lon, lat, ...]``, no node elements) instead of the
        Overpass node/way/relation shape: no node dicts are ever built.
        """
        db = self._conn()
        sql_len = min_len_m > 0 and self.has_len
        len_clause = " AND w.len_m >= ?" if sql_len else ""
        ways: dict[int, tuple[str, bytes, bytes]] = {}
        rels: dict[int, tuple[str, str]] = {}
        for m in members:
            bit = 1 << self._bit[(m.kind, m.filter)]
            south, west, north, east = m.bbox
            args = (north, south, east, west, bit)
            if m.kind == "way":
                for wid, tags, nodes, coords in db.execute(
                    "SELECT w.id, w.tags, w.nodes, w.coords FROM way_idx i "
                    "JOIN ways w ON w.id = i.id "
                    "WHERE i.min_lat <= ? AND i.max_lat >= ? "
                    "AND i.min_lon <= ? AND i.max_lon >= ? AND (w.sel & ?) != 0"
                    + len_clause,
                    args + ((min_len_m,) if sql_len else ()),
                ):
                    ways[wid] = (tags, nodes, coords)
            else:
                for rid, tags, members_json in db.execute(
                    "SELECT r.id, r.tags, r.members FROM rel_idx i "
                    "JOIN relations r ON r.id = i.id "
                    "WHERE i.min_lat <= ? AND i.max_lat >= ? "
                    "AND i.min_lon <= ? AND i.max_lon >= ? AND (r.sel & ?) != 0",
                    args,
                ):
                    rels[rid] = (tags, members_json)

        # ``>`` recursion: every member way of a hit relation comes along.
        need = {
            ref for _, mj in rels.values() for ref, _role in json.loads(mj)
            if ref not in ways
        }
        needed = sorted(need)
        for i in range(0, len(needed), _CHUNK):
            part = needed[i : i + _CHUNK]
            marks = ",".join("?" * len(part))
            for wid, tags, nodes, coords in db.execute(
                f"SELECT id, tags, nodes, coords FROM ways w WHERE id IN ({marks})"
                + len_clause,
                part + ([min_len_m] if sql_len else []),
            ):
                ways[wid] = (tags, nodes, coords)

        elements: list[dict] = []
        for rid, (tags, mj) in rels.items():
            elements.append({
                "type": "relation", "id": rid, "tags": json.loads(tags),
                "members": [
                    {"type": "way", "ref": ref, "role": role}
                    for ref, role in json.loads(mj)
                ],
            })
        if compact:
            for wid, (tags, _nodes_blob, coords_blob) in ways.items():
                flat = _unpack("i", coords_blob)
                g = [0.0] * len(flat)
                g[0::2] = [v / _E7 for v in flat[1::2]]  # lon
                g[1::2] = [v / _E7 for v in flat[0::2]]  # lat
                elements.append({"type": "way", "id": wid, "tags": json.loads(tags), "g": g})
        else:
            nodes_out: dict[int, tuple[int, int]] = {}
            for wid, (tags, nodes_blob, coords_blob) in ways.items():
                ids = _unpack("q", nodes_blob)
                flat = _unpack("i", coords_blob)
                for k, nid in enumerate(ids):
                    if nid not in nodes_out:
                        nodes_out[nid] = (flat[2 * k], flat[2 * k + 1])
                elements.append({
                    "type": "way", "id": wid, "nodes": list(ids), "tags": json.loads(tags),
                })
            for nid, (lat, lon) in nodes_out.items():
                elements.append({"type": "node", "id": nid, "lat": lat / _E7, "lon": lon / _E7})
        if min_len_m > 0 and not self.has_len:
            elements = drop_short_ways(elements, min_len_m)  # schema 1 file
        return {
            "version": 0.6,
            "generator": "fund-vista-osm-local",
            "osm3s": {"timestamp_osm_base": self.meta.get("osm_timestamp", "")},
            "elements": elements,
        }

    def health(self) -> dict:
        rings = self.rings
        return {
            "status": "ok" if self.fresh else "stale",
            "stale_reason": None if self.fresh else "layers changed since build; rebuild",
            "source": self.meta.get("source"),
            "osm_timestamp": self.meta.get("osm_timestamp"),
            "ways": int(self.meta.get("ways", 0)),
            "relations": int(self.meta.get("relations", 0)),
            "coverage_bounds_swne": cov.poly_bounds(rings),
        }
