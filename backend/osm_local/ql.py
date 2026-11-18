"""Strict parser for the one Overpass QL shape citymap sends.

``wrap_query`` produces ``[out:json][timeout:N]; ( member; ... ); out body;
>; out skel qt;`` where every member is ``way|relation`` + tag filters +
``(south,west,north,east);``. Anything else is rejected, never guessed at -
the caller falls back to a real Overpass mirror.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NUM = r"-?\d+(?:\.\d+)?"
_MEMBER = (
    r"(?P<kind>way|relation)(?P<filter>(?:\[[^\]\n]*\])*)"
    rf"\((?P<s>{_NUM}),(?P<w>{_NUM}),(?P<n>{_NUM}),(?P<e>{_NUM})\);"
)
_ENVELOPE = re.compile(
    r"\[out:json\]\[timeout:\d+\];\s*\(\s*(?P<body>.*?)\s*\);\s*"
    r"out body;\s*>;\s*out skel qt;\s*",
    re.S,
)
_MEMBER_RE = re.compile(_MEMBER)
_MEMBERS_ONLY = re.compile(rf"(?:\s*{_MEMBER})+\s*")

BBox = tuple[float, float, float, float]  # south, west, north, east


class QueryError(ValueError):
    """The query is not the citymap shape (answer 400, let a mirror take it)."""


@dataclass(frozen=True)
class Member:
    kind: str  # "way" | "relation"
    filter: str  # e.g. '["highway"~"^(motorway)$"]'
    bbox: BBox


def parse(query: str) -> list[Member]:
    env = _ENVELOPE.fullmatch(query.strip())
    if not env:
        raise QueryError("unsupported query envelope")
    body = env.group("body")
    if not _MEMBERS_ONLY.fullmatch(body):
        raise QueryError("unsupported union member")
    members = [
        Member(
            m["kind"], m["filter"],
            (float(m["s"]), float(m["w"]), float(m["n"]), float(m["e"])),
        )
        for m in _MEMBER_RE.finditer(body)
    ]
    if not members:
        raise QueryError("empty union")
    return members


# --- the airport shape: ``(way|relation)[filter](around:r,lat,lon);`` + ``out geom;``

_AROUND_MEMBER = (
    r"(?P<kind>way|relation)(?P<filter>(?:\[[^\]\n]*\])*)"
    rf"\(around:(?P<r>\d+),(?P<lat>{_NUM}),(?P<lon>{_NUM})\);"
)
_AROUND_ENVELOPE = re.compile(
    r"\[out:json\]\[timeout:\d+\];\s*\(\s*(?P<body>.*?)\s*\);\s*out geom;\s*", re.S,
)
_AROUND_RE = re.compile(_AROUND_MEMBER)
_AROUND_ONLY = re.compile(rf"(?:\s*{_AROUND_MEMBER})+\s*")


@dataclass(frozen=True)
class AroundMember:
    kind: str
    filter: str
    lat: float
    lon: float
    radius_m: int


def parse_around(query: str) -> list[AroundMember]:
    """Airport-style union of ``around`` members answered with ``out geom``."""
    env = _AROUND_ENVELOPE.fullmatch(query.strip())
    if not env:
        raise QueryError("unsupported query envelope")
    body = env.group("body")
    if not _AROUND_ONLY.fullmatch(body):
        raise QueryError("unsupported union member")
    members = [
        AroundMember(m["kind"], m["filter"], float(m["lat"]), float(m["lon"]), int(m["r"]))
        for m in _AROUND_RE.finditer(body)
    ]
    if not members:
        raise QueryError("empty union")
    return members
