"""Selector registry: the Overpass tag filters citymap can ask for.

The registry is derived from :data:`backend.citymap.layers.LAYERS`, so the
extract only ever has to answer the filters the app really sends. Each
``(kind, filter_text)`` pair owns one bit; ways/relations are tagged with
the bits they match at build time and queried by bit at request time. The
registry is stored in the database; if ``layers.py`` changes the server
reports the file stale and unknown filters get a ``400`` (public mirrors
answer instead) until it is rebuilt.
"""

from __future__ import annotations

import re
from functools import lru_cache

from backend.citymap.layers import LAYERS

Selector = tuple[str, str]  # (kind "way"|"relation", filter text e.g. '["building"]')

_PRED = re.compile(r'\["([^"\]]+)"(?:(=|~)"((?:[^"\\]|\\.)*)")?\]')
_BBOX_SUFFIX = "({bbox})"


#: Selectors beyond the citymap layers, APPENDED after them so a database
#: built before they existed stays valid for everything it already knows
#: (``Store.fresh`` accepts a stored registry that is a prefix of this one).
#: Airports draw aeroway relations (multipolygon aprons/terminals).
EXTRA_SELECTORS: tuple[Selector, ...] = (("relation", '["aeroway"]'),)

#: ``around`` queries (airports, airport traffic): the exact filter text the
#: caller sends -> the registry selectors whose stored ways/relations stand in
#: for it. Matches are re-checked against the filter, so a narrower filter
#: (the reduced aeroway regex) is exact. A *wider* filter (``["highway"]``)
#: omits ways the registry never stored (``highway=construction`` ...); the
#: airport splitters drop those anyway, and a test pins that (see
#: ``test_osm_local_geom``). Anything not listed here is not answered locally.
_HW = (
    ("way", '["highway"~"^(motorway|trunk|primary)(_link)?$"]'),
    ("way", '["highway"~"^(secondary|tertiary|unclassified|residential|living_street|pedestrian|service|track|road)(_link)?$"]'),
    ("way", '["highway"~"^(footway|path|cycleway|steps|bridleway|corridor)$"]'),
)
_AERO_W = (("way", '["aeroway"]'),)
_AERO_R = (("relation", '["aeroway"]'),)
AROUND_FILTERS: dict[Selector, tuple[Selector, ...]] = {
    ("way", '["aeroway"]'): _AERO_W,
    ("relation", '["aeroway"]'): _AERO_R,
    ("way", '["aeroway"~"^(parking_position|hangar|terminal)$"]'): _AERO_W,
    ("relation", '["aeroway"~"^(parking_position|hangar|terminal)$"]'): _AERO_R,
    ("way", '["highway"]'): _HW,
    ("way", '["railway"]'): (("way", '["railway"~"^(rail|light_rail|subway|tram|narrow_gauge|monorail|preserved)$"]'),),
    ("way", '["building"]'): (("way", '["building"]'),),
    ("way", '["natural"="water"]'): (("way", '["natural"="water"]'),),
    ("way", '["water"]'): (("way", '["water"]'),),
    ("way", '["waterway"]'): (("way", '["waterway"~"^(river|stream|canal|ditch|drain)$"]'),),
}


def registry() -> list[Selector]:
    """Every distinct selector in layer order, then the extras (stable across calls)."""
    out: list[Selector] = []
    for entry in LAYERS.values():
        for text in list(entry["way_selectors"]) + list(entry["relation_selectors"]):  # type: ignore[arg-type]
            if not text.endswith(_BBOX_SUFFIX):
                raise ValueError(f"selector without bbox suffix: {text!r}")
            body = text[: -len(_BBOX_SUFFIX)]
            kind, _, rest = body.partition("[")
            sel = (kind, "[" + rest)
            if sel not in out:
                out.append(sel)
    for sel in EXTRA_SELECTORS:
        if sel not in out:
            out.append(sel)
    return out


@lru_cache(maxsize=256)
def compile_filter(text: str) -> tuple[tuple[str, str, re.Pattern[str] | str | None], ...]:
    """Parse ``["k"]["k"="v"]["k"~"re"]`` into predicates; ValueError if not fully understood."""
    preds: list[tuple[str, str, re.Pattern[str] | str | None]] = []
    pos = 0
    while pos < len(text):
        m = _PRED.match(text, pos)
        if not m:
            raise ValueError(f"unsupported filter: {text!r}")
        key, op, val = m.group(1), m.group(2) or "", m.group(3)
        if op == "~":
            preds.append((key, op, re.compile(val or "")))
        else:
            preds.append((key, op, val))
        pos = m.end()
    return tuple(preds)


def tags_match(filter_text: str, tags: dict[str, str]) -> bool:
    for key, op, val in compile_filter(filter_text):
        have = tags.get(key)
        if have is None:
            return False
        if op == "=" and have != val:
            return False
        if op == "~" and not val.search(have):  # type: ignore[union-attr]
            return False
    return True


def selector_mask(kind: str, tags: dict[str, str], sels: list[Selector]) -> int:
    """Bitmask of the ``kind`` selectors in ``sels`` that ``tags`` satisfy."""
    mask = 0
    for bit, (k, text) in enumerate(sels):
        if k == kind and tags_match(text, tags):
            mask |= 1 << bit
    return mask
