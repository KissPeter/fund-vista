"""Aircraft type → size category (Phase 5, plan §4 items 1+4).

Maps ICAO aircraft type designators (the adsb.lol ``t`` field) to one of
four plotter pens: wide-body / narrow-body / regional / other. The table
covers the types normally seen at the 29 Central-Europe airports; anything
unknown or missing (e.g. helicopters, business jets, gliders, empty field)
falls into ``other`` — the filter never drops a movement for an
unrecognised code, it just draws it in the grey pen.

Pure module: no app import, no network (test-design skill).
"""

from __future__ import annotations

CATEGORIES = ("wide", "narrow", "regional", "other")

_WIDE = frozenset({
    "A310", "A332", "A333", "A338", "A339",
    "A342", "A343", "A345", "A346", "A359", "A35K", "A388",
    "B744", "B748", "B763", "B772", "B773", "B77W",
    "B788", "B789", "B78X", "MD11",
})

_NARROW = frozenset({
    "A318", "A319", "A320", "A321", "A19N", "A20N", "A21N",
    "BCS1", "BCS3",
    "B733", "B734", "B735", "B736", "B737", "B738", "B739",
    "B38M", "B39M", "B752", "B753",
    "MD81", "MD82", "MD83", "MD87", "MD88", "MD90", "B717",
})

_REGIONAL = frozenset({
    "E170", "E175", "E190", "E195", "E290", "E295",
    "CRJ1", "CRJ2", "CRJ7", "CRJ9", "CRJX",
    "AT43", "AT72", "DH8A", "DH8B", "DH8C", "DH8D",
    "SF34", "E120", "F50", "F70", "F100", "J328",
})

_TYPE_CATEGORY = {code: "wide" for code in _WIDE}
_TYPE_CATEGORY.update({code: "narrow" for code in _NARROW})
_TYPE_CATEGORY.update({code: "regional" for code in _REGIONAL})


def category_of(type_code: str | None) -> str:
    """Size category for an ICAO type designator (``other`` when unknown)."""
    return _TYPE_CATEGORY.get((type_code or "").strip().upper(), "other")


__all__ = ["CATEGORIES", "category_of"]
