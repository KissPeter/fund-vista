"""Pen and runway colours baked into the artwork (compose-sheet colour fields).

The shop used to recolour the preview in the browser. The backend now does it
before composing, so the preview PNG and the SVG agree. Only paint values
change: ``fill`` and ``stroke`` attributes that hold a solid colour. Geometry,
ids and structure are untouched.

Rules (RFC2119 wording is in ``docs/F-007-gallery-sheet-compose.md``):

- ``pen`` replaces every solid ``fill``/``stroke`` colour of the artwork, except
  white (paper), ``none``, ``transparent``, ``currentColor``, ``inherit`` and
  ``url(...)`` references.
- ``runway`` replaces the same paint inside any element whose ``id`` contains
  ``runway`` (``runways``, ``osm-runways``, ``runway-marks``), including the
  descendants of such an element. When ``runway`` is absent those elements take
  ``pen``.
- ``style="..."`` declarations are not rewritten; the artwork ships paint as
  attributes (see ``backend/airports/render.py``).

When both colours are absent the input is returned unchanged, so the order file
(``artwork="inline"``, no colours) stays byte-identical.
"""

from __future__ import annotations

import re

_TAG_RE = re.compile(r"<(/?)([A-Za-z][\w:.-]*)([^<>]*)>")
_PAINT_RE = re.compile(r"""(?<![\w-])(fill|stroke)(\s*=\s*)(["'])([^"']*)\3""")
_ID_RE = re.compile(r"""(?<![\w-])id\s*=\s*(["'])([^"']*)\1""")
_KEEP = frozenset({"", "none", "transparent", "currentcolor", "inherit"})
_PAPER = frozenset({"#fff", "#ffffff", "white"})


def _is_recolourable(value: str) -> bool:
    v = value.strip().lower()
    return v not in _KEEP and not v.startswith("url(") and v not in _PAPER


def _recolour_tag(tag: str, colour: str) -> str:
    def paint(match: re.Match[str]) -> str:
        attr, eq, quote, value = match.group(1), match.group(2), match.group(3), match.group(4)
        if not _is_recolourable(value):
            return match.group(0)
        return f"{attr}{eq}{quote}{colour}{quote}"

    return _PAINT_RE.sub(paint, tag)


def recolour_artwork(svg: str, *, pen: str | None = None, runway: str | None = None) -> str:
    """Return ``svg`` with its paint replaced by ``pen`` and ``runway``.

    ``pen`` and ``runway`` are validated ``#rrggbb`` strings (the request model
    checks them). With neither set, the input is returned as it is.
    """
    if pen is None and runway is None:
        return svg
    # Stack of "inside a runway element" flags, one per open element.
    stack: list[bool] = []
    out: list[str] = []
    last = 0
    for match in _TAG_RE.finditer(svg):
        closing, _name, rest = match.group(1), match.group(2), match.group(3)
        out.append(svg[last:match.start()])
        last = match.end()
        tag = match.group(0)
        if closing:
            if stack:
                stack.pop()
            out.append(tag)
            continue
        self_closing = rest.rstrip().endswith("/")
        id_match = _ID_RE.search(rest)
        own_runway = bool(id_match) and "runway" in id_match.group(2).lower()
        inside = (stack[-1] if stack else False) or own_runway
        colour = runway if inside and runway is not None else pen
        if colour is not None:
            tag = _recolour_tag(tag, colour)
        if not self_closing:
            stack.append(inside)
        out.append(tag)
    out.append(svg[last:])
    return "".join(out)
