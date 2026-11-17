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


def registry() -> list[Selector]:
    """Every distinct selector in layer order (stable across calls)."""
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
