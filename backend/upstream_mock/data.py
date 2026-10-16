"""Deterministic synthetic upstream data for ``FUNDVISTA_UPSTREAM_MODE=mock``.

Everything here is generated, not recorded: tiny but structurally valid
payloads in the exact shapes the real upstreams return (Overpass JSON,
OSM API XML, Nominatim JSON, OurAirports CSV, GitHub release JSON and an
adsb.lol-style trace tar). No network, no randomness, no clock — the same
request always yields byte-identical output.
"""

from __future__ import annotations

import gzip
import io
import json
import math
import tarfile
import unicodedata
import zlib
from xml.sax.saxutils import quoteattr

# --------------------------------------------------------------- geocoding

# (all-of match keys, display_name, south, west, north, east, class, type)
_PLACES: list[tuple[tuple[str, ...], str, float, float, float, float, str, str]] = [
    (("dom ter", "szeged"), "Dóm tér, Belváros, Szeged, Csongrád-Csanád, Hungary",
     46.2515, 20.1470, 46.2535, 20.1500, "place", "square"),
    (("szeged",), "Szeged, Csongrád-Csanád, Hungary",
     46.1900, 20.0400, 46.3200, 20.2200, "boundary", "administrative"),
    (("budapest",), "Budapest, Central Hungary, Hungary",
     47.3492, 18.9251, 47.6136, 19.3333, "boundary", "administrative"),
    (("debrecen",), "Debrecen, Hajdú-Bihar, Hungary",
     47.4400, 21.5200, 47.6100, 21.7600, "boundary", "administrative"),
    (("pecs",), "Pécs, Baranya, Hungary",
     45.9800, 18.1700, 46.1200, 18.3100, "boundary", "administrative"),
    (("london",), "London, Greater London, England, United Kingdom",
     51.2868, -0.5103, 51.6919, 0.3340, "boundary", "administrative"),
    (("e2e",), "Dóm tér E2E, Mockváros, Test County, Hungary",
     47.5000, 19.0000, 47.5400, 19.0500, "place", "square"),
    (("mockvaros",), "Mockváros, Test County, Hungary",
     47.5000, 19.0000, 47.5400, 19.0500, "place", "city"),
]


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text)
    return "".join(c for c in folded if not unicodedata.combining(c)).lower().strip()


def nominatim_results(query: str, limit: int) -> list[dict]:
    """Gazetteer hits for ``query``; best (most specific) match first, ``[]`` when unknown."""
    q = _norm(query)
    hits = [p for p in _PLACES if all(k in q for k in p[0])]
    hits.sort(key=lambda p: -len(p[0]))
    out: list[dict] = []
    for _keys, name, s, w, n, e, cls, typ in hits:
        out.append({
            "place_id": 1000 + len(out),
            "display_name": name,
            "boundingbox": [f"{s:.7f}", f"{n:.7f}", f"{w:.7f}", f"{e:.7f}"],
            "lat": f"{(s + n) / 2:.7f}",
            "lon": f"{(w + e) / 2:.7f}",
            "class": cls,
            "type": typ,
        })
    return out[:limit]


# ------------------------------------------------------------ OurAirports

# ident, type, name, lat, lon, elev_ft, country, municipality, iata,
# runway (le_ident, he_ident, length_ft, heading_deg)
_AIRPORTS = [
    ("LHBP", "large_airport", "Budapest Liszt Ferenc International Airport", 47.4369, 19.2556, 495, "HU", "Budapest", "BUD", ("13L", "31R", 12162, 130)),
    ("LHDC", "medium_airport", "Debrecen International Airport", 47.4889, 21.6153, 359, "HU", "Debrecen", "DEB", ("13", "31", 8202, 130)),
    ("LHPP", "small_airport", "Pécs-Pogány International Airport", 45.9909, 18.2410, 656, "HU", "Pécs", "PEV", ("16", "34", 6561, 160)),
    ("LHSM", "small_airport", "Hévíz-Balaton Airport", 46.6864, 17.1591, 413, "HU", "Sármellék", "SOB", ("16", "34", 8202, 160)),
    ("LHUD", "small_airport", "Szeged Airport", 46.2493, 20.0995, 270, "HU", "Szeged", "", ("04", "22", 3281, 40)),
    ("EGLL", "large_airport", "London Heathrow Airport", 51.4706, -0.4619, 83, "GB", "London", "LHR", ("09L", "27R", 12802, 90)),
]


def _csv(rows: list[list[object]]) -> str:
    def cell(v: object) -> str:
        s = "" if v is None else str(v)
        return '"' + s.replace('"', '""') + '"' if ("," in s or '"' in s) else s

    return "\n".join(",".join(cell(c) for c in r) for r in rows) + "\n"


def ourairports_csv(name: str) -> str | None:
    """CSV text for ``airports`` / ``runways`` / ``airport-frequencies``; ``None`` otherwise."""
    if name == "airports":
        rows: list[list[object]] = [[
            "id", "ident", "type", "name", "latitude_deg", "longitude_deg",
            "elevation_ft", "continent", "iso_country", "iso_region",
            "municipality", "scheduled_service", "gps_code", "iata_code",
            "local_code", "home_link", "wikipedia_link", "keywords",
        ]]
        for i, a in enumerate(_AIRPORTS, start=1):
            ident, typ, nm, lat, lon, elev, cc, muni, iata, _ = a
            rows.append([i, ident, typ, nm, lat, lon, elev, "EU", cc, f"{cc}-XX",
                         muni, "yes", ident, iata, "", "", "", ""])
        return _csv(rows)
    if name == "runways":
        rows = [[
            "id", "airport_ref", "airport_ident", "length_ft", "width_ft",
            "surface", "lighted", "closed", "le_ident", "le_latitude_deg",
            "le_longitude_deg", "le_elevation_ft", "le_heading_degT",
            "le_displaced_threshold_ft", "he_ident", "he_latitude_deg",
            "he_longitude_deg", "he_elevation_ft", "he_heading_degT",
            "he_displaced_threshold_ft",
        ]]
        for i, a in enumerate(_AIRPORTS, start=1):
            ident, _t, _n, lat, lon, elev, _c, _m, _i, (le, he, length, hdg) = a
            half_m = length * 0.3048 / 2
            dlat = half_m * math.cos(math.radians(hdg)) / 110540.0
            dlon = half_m * math.sin(math.radians(hdg)) / (111320.0 * math.cos(math.radians(lat)))
            rows.append([i, i, ident, length, 148, "ASP", 1, 0,
                         le, round(lat - dlat, 6), round(lon - dlon, 6), elev, hdg, "",
                         he, round(lat + dlat, 6), round(lon + dlon, 6), elev, (hdg + 180) % 360, ""])
        return _csv(rows)
    if name == "airport-frequencies":
        rows = [["id", "airport_ref", "airport_ident", "type", "description", "frequency_mhz"]]
        n = 0
        for i, a in enumerate(_AIRPORTS, start=1):
            for typ, desc, mhz in (("TWR", "Tower", 118.1), ("GND", "Ground", 121.9), ("ATIS", "ATIS", 127.4)):
                n += 1
                rows.append([n, i, a[0], typ, f"{a[2].split()[0]} {desc}", mhz])
        return _csv(rows)
    return None


# ------------------------------------------------ OSM geometry (synthetic)

Pt = tuple[float, float]  # (lat, lon)
BBox = tuple[float, float, float, float]  # south, west, north, east


def _frac(bbox: BBox, fy: float, fx: float) -> Pt:
    s, w, n, e = bbox
    return (s + (n - s) * fy, w + (e - w) * fx)


def _ring(bbox: BBox, fy: float, fx: float, size: float) -> list[Pt]:
    pts = [(fy, fx), (fy, fx + size), (fy + size, fx + size), (fy + size, fx), (fy, fx)]
    return [_frac(bbox, a, b) for a, b in pts]


def bbox_ways(bbox: BBox, layers: set[str]) -> list[tuple[str, dict, list[Pt]]]:
    """``(key, tags, points)`` ways for ``layers`` inside ``bbox`` (s, w, n, e).

    ``layers`` uses the citymap layer ids. The layout is a fixed set of
    fractions of the bbox so framing assertions are stable: a bottom-left to
    top-right motorway diagonal, a 5x5 street grid, a rail line, a river, a
    lake and a few buildings.
    """
    out: list[tuple[str, dict, list[Pt]]] = []
    if "highways" in layers:
        out.append(("hw", {"highway": "primary"}, [_frac(bbox, 0.95, 0.05), _frac(bbox, 0.5, 0.5), _frac(bbox, 0.05, 0.95)]))
    if "roads" in layers:
        for i in range(5):
            f = 0.1 + i * 0.2
            out.append((f"rh{i}", {"highway": "residential"}, [_frac(bbox, f, 0.05), _frac(bbox, f, 0.95)]))
            out.append((f"rv{i}", {"highway": "residential"}, [_frac(bbox, 0.05, f), _frac(bbox, 0.95, f)]))
    if "paths" in layers:
        out.append(("pa", {"highway": "footway"}, [_frac(bbox, 0.2, 0.2), _frac(bbox, 0.3, 0.35), _frac(bbox, 0.25, 0.5)]))
    if "rails" in layers:
        out.append(("rl", {"railway": "rail"}, [_frac(bbox, 0.3, 0.02), _frac(bbox, 0.35, 0.5), _frac(bbox, 0.3, 0.98)]))
    if "waterway" in layers:
        out.append(("ww", {"waterway": "river"}, [_frac(bbox, 0.02, 0.7), _frac(bbox, 0.5, 0.8), _frac(bbox, 0.98, 0.7)]))
    if "water" in layers:
        out.append(("wa", {"natural": "water"}, _ring(bbox, 0.6, 0.1, 0.15)))
    if "buildings" in layers:
        for i in range(6):
            out.append((f"bu{i}", {"building": "yes"}, _ring(bbox, 0.12 + 0.13 * i, 0.12 + 0.12 * (i % 3), 0.05)))
    if "aeroway" in layers:
        out.append(("ae", {"aeroway": "runway"}, [_frac(bbox, 0.4, 0.1), _frac(bbox, 0.45, 0.9)]))
    return out


def _stable_id(*parts: object) -> int:
    return zlib.crc32("|".join(str(p) for p in parts).encode("utf-8")) & 0x3FFFFFFF


def overpass_bbox_elements(bboxes: list[BBox], layers: set[str]) -> list[dict]:
    """Overpass ``out body; >; out skel qt;`` shaped elements (node refs)."""
    nodes: dict[int, dict] = {}
    ways: dict[int, dict] = {}
    for bb in bboxes:
        for key, tags, pts in bbox_ways(bb, layers):
            ids = []
            for lat, lon in pts:
                nid = _stable_id("n", round(lat, 6), round(lon, 6))
                nodes[nid] = {"type": "node", "id": nid, "lat": round(lat, 6), "lon": round(lon, 6)}
                ids.append(nid)
            wid = _stable_id("w", key, bb)
            ways[wid] = {"type": "way", "id": wid, "nodes": ids, "tags": tags}
    return [*ways.values(), *nodes.values()]


def osm_api_xml(bbox_wsen: tuple[float, float, float, float]) -> str:
    """OSM v0.6 ``/map`` XML for a ``west,south,east,north`` bbox."""
    w, s, e, n = bbox_wsen
    elements = overpass_bbox_elements(
        [(s, w, n, e)],
        {"highways", "roads", "paths", "rails", "waterway", "water", "buildings"},
    )
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6" generator="fundvista-mock">']
    for el in elements:
        if el["type"] == "node":
            parts.append(f'<node id="{el["id"]}" lat="{el["lat"]}" lon="{el["lon"]}"/>')
    for el in elements:
        if el["type"] == "way":
            nds = "".join(f'<nd ref="{r}"/>' for r in el["nodes"])
            tags = "".join(f"<tag k={quoteattr(k)} v={quoteattr(v)}/>" for k, v in el["tags"].items())
            parts.append(f'<way id="{el["id"]}">{nds}{tags}</way>')
    parts.append("</osm>")
    return "\n".join(parts)


def _offset(lat: float, lon: float, east_m: float, north_m: float) -> Pt:
    return (
        lat + north_m / 110540.0,
        lon + east_m / (111320.0 * math.cos(math.radians(lat))),
    )


def _geom(pts: list[Pt]) -> list[dict]:
    return [{"lat": round(a, 7), "lon": round(b, 7)} for a, b in pts]


def _rot(along: float, across: float, hdg_deg: float) -> tuple[float, float]:
    """Runway-frame (along, across) metres to east/north metres."""
    h = math.radians(hdg_deg)
    return (along * math.sin(h) + across * math.cos(h), along * math.cos(h) - across * math.sin(h))


def airport_geom_elements(lat: float, lon: float) -> list[dict]:
    """``out geom`` aeroway ways around an airport centre (runway, taxiways, apron, terminal)."""
    hdg = 130.0

    def way(key: str, tags: dict, along_across: list[tuple[float, float]]) -> dict:
        pts = []
        for a, c in along_across:
            e, n = _rot(a, c, hdg)
            pts.append(_offset(lat, lon, e, n))
        return {"type": "way", "id": _stable_id("ap", key, round(lat, 4), round(lon, 4)),
                "tags": tags, "geometry": _geom(pts)}

    return [
        way("rwy", {"aeroway": "runway", "ref": "13/31"}, [(-1500, 0), (1500, 0)]),
        way("twy-a", {"aeroway": "taxiway", "ref": "A"}, [(-1400, 160), (1400, 160)]),
        way("twy-a1", {"aeroway": "taxiway", "ref": "A1"}, [(-1000, 160), (-1000, 0)]),
        way("twy-b2", {"aeroway": "taxiway", "ref": "B2"}, [(200, 160), (200, 0)]),
        way("apron", {"aeroway": "apron"}, [(-300, 200), (300, 200), (300, 450), (-300, 450), (-300, 200)]),
        way("term", {"aeroway": "terminal", "building": "yes"}, [(-150, 470), (150, 470), (150, 540), (-150, 540), (-150, 470)]),
        way("stand1", {"aeroway": "parking_position"}, [(-200, 300), (-180, 300)]),
    ]


def airport_context_elements(lat: float, lon: float) -> list[dict]:
    """``out geom`` street/rail/building/water context ways around the centre."""

    def way(key: str, tags: dict, en: list[tuple[float, float]]) -> dict:
        pts = [_offset(lat, lon, e, n) for e, n in en]
        return {"type": "way", "id": _stable_id("ctx", key, round(lat, 4), round(lon, 4)),
                "tags": tags, "geometry": _geom(pts)}

    return [
        way("hw", {"highway": "primary"}, [(-2500, -1200), (0, -1100), (2500, -1200)]),
        way("rd", {"highway": "residential"}, [(-1800, 1500), (1800, 1500)]),
        way("rl", {"railway": "rail"}, [(-2500, 1900), (2500, 2100)]),
        way("bu", {"building": "yes"}, [(900, 900), (960, 900), (960, 960), (900, 960), (900, 900)]),
        way("wa", {"natural": "water"}, [(-2200, -400), (-2000, -400), (-2000, -200), (-2200, -200), (-2200, -400)]),
        way("ww", {"waterway": "river"}, [(-2500, -2000), (0, -1800), (2500, -2000)]),
    ]


# ------------------------------------------------------- traffic archive

def _flight(icao: str, reg: str, typ: str, call: str, lat: float, lon: float, elev_ft: float, arrival: bool) -> dict:
    n = 40
    rows = []
    for i in range(n):
        f = 1 - i / (n - 1) if arrival else i / (n - 1)
        d = 40.0 * f / 111.2
        alt = "ground" if f == 0 else round(elev_ft + 300 + 9000 * f)
        row: list[object] = [i * 10.0, round(lat + d * 0.7, 5), round(lon - d * 0.7, 5), alt, 0, 0, 0, 0]
        if i == 0:
            row.append({"flight": f"{call:<8}"})
        rows.append(row)
    return {"icao": icao, "r": reg, "t": typ, "desc": typ, "dbFlags": 0, "ownOp": None,
            "timestamp": 1_000_000.0, "trace": rows}


_TAR_CACHE: dict[str, bytes] = {}


def trace_tar() -> bytes:
    """Tar of gzip ``trace_full_*.json`` docs: one arrival + one departure per Hungarian airport."""
    if "tar" in _TAR_CACHE:
        return _TAR_CACHE["tar"]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        k = 0
        for _ident, _t, _n, lat, lon, elev, cc, _m, _i, _r in _AIRPORTS:
            if cc != "HU":
                continue
            for arrival, typ, call in ((True, "A320", "WZZ"), (False, "B738", "RYR")):
                k += 1
                doc = _flight(f"a{k:05x}", f"HA-L{k:03d}", typ, f"{call}{k}", lat, lon, float(elev), arrival)
                raw = gzip.compress(json.dumps(doc, separators=(",", ":")).encode(), mtime=0)
                info = tarfile.TarInfo(f"traces/{k % 256:02x}/trace_full_{doc['icao']}.json")
                info.size = len(raw)
                info.mtime = 0
                tf.addfile(info, io.BytesIO(raw))
    _TAR_CACHE["tar"] = buf.getvalue()
    return _TAR_CACHE["tar"]


def release_json(tag: str, repo: str) -> dict:
    """GitHub ``releases/tags/<tag>`` payload with one downloadable archive asset."""
    url = f"https://github.com/{repo}/releases/download/{tag}/{tag}.tar"
    return {"tag_name": tag, "assets": [{"name": f"{tag}.tar", "size": len(trace_tar()), "browser_download_url": url}]}
