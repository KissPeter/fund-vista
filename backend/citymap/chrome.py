"""Strip city-roads SVG chrome: location caption + OSM credit.

SVGs exported by the upstream city-roads app
(``scene.saveToSVG``, ``src/lib/svgExport.js``) always carry two extras
the plotter must not draw:

1. a leading XML comment with the generator URL and the
   ``Data © OpenStreetMap contributors`` credit, and
2. trailing ``<text>`` caption elements (the location name such as
   "Budapest", plus the data-source line).

:func:`strip_city_roads_chrome` removes both and returns the cleaned
document plus what was removed, so callers can log it. Geometry
(``<path>``/``<g>``) is never touched. Our own renderer
(:mod:`backend.citymap.render`) is chrome-free by construction and never
needs this — it exists for SVGs imported from the city-roads app.

Licence note: removing the credit from the *artwork* does not remove the
obligation — OSM data is ODbL 1.0 and public use requires attribution.
Keep the ``attribution`` response field / UI credit when serving maps.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)

_COMMENT_MARKERS = ("openstreetmap", "city-roads", "generator")

# Fallback regexes when the document is not well-formed XML.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TEXT_RE = re.compile(r"<text\b[^>]*>.*?</text\s*>", re.DOTALL | re.IGNORECASE)


def _strip_with_xml(svg_text: str) -> tuple[str, list[str]] | None:
    """ElementTree pass. Returns None when the document won't parse."""
    try:
        root = ET.fromstring(svg_text.encode("utf-8"))
    except ET.ParseError:
        return None
    removed: list[str] = []

    def local(tag: str) -> str:
        return tag.split("}")[-1].lower() if isinstance(tag, str) else ""

    # Keep the default SVG namespace unprefixed on serialize — otherwise
    # ElementTree invents ns0: prefixes and downstream "<path" searches miss.
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    # Drop caption texts (city-roads appends them as bare <text> nodes).
    for parent in root.iter():
        for child in list(parent):
            if local(child.tag) == "text":
                removed.append("".join(child.itertext()).strip())
                parent.remove(child)
    # Drop generator/credit comments anywhere in the tree. (A comment
    # placed *before* the root element never reaches the tree at all —
    # the XML parser drops prolog comments when ET.fromstring runs.)
    for parent in root.iter():
        for child in list(parent):
            if child.tag is ET.Comment and any(
                m in (child.text or "").lower() for m in _COMMENT_MARKERS
            ):
                first_line = (child.text or "").strip().splitlines()
                removed.append(first_line[0][:80] if first_line else "<comment>")
                parent.remove(child)
    return ET.tostring(root, encoding="unicode") + "\n", removed


def strip_city_roads_chrome(svg_text: str) -> tuple[str, list[str]]:
    """Remove location caption + OSM credit from a city-roads SVG.

    Returns ``(cleaned_svg, removed_captions)``. Idempotent: running it on
    an already-clean document is a no-op returning ``([], )`` removals.
    """
    # Prolog comments (the generator/credit block city-roads emits before
    # the root element) never reach the ElementTree — the XML parser drops
    # them. Strip + report them up front so they are counted, not silent.
    pre_removed: list[str] = []

    def _pre_strip(match: re.Match) -> str:
        text = match.group(0)
        if any(mk in text.lower() for mk in _COMMENT_MARKERS):
            first = text.strip().splitlines()
            pre_removed.append(first[0][:80] if first else "<comment>")
            return ""
        return text

    body = _COMMENT_RE.sub(_pre_strip, svg_text)
    parsed = _strip_with_xml(body)
    if parsed is not None:
        cleaned, removed = parsed
        removed = pre_removed + removed
        if removed:
            log.info("citymap.chrome stripped %d caption(s): %s", len(removed), removed[:3])
        return cleaned, removed
    # Regex fallback for malformed exports.
    removed_comments = [
        m.group(0)[:80]
        for m in _COMMENT_RE.finditer(svg_text)
        if any(mk in m.group(0).lower() for mk in _COMMENT_MARKERS)
    ]
    removed_texts = [m.group(0)[:80] for m in _TEXT_RE.finditer(svg_text)]
    cleaned = _COMMENT_RE.sub(
        lambda m: "" if any(mk in m.group(0).lower() for mk in _COMMENT_MARKERS) else m.group(0),
        svg_text,
    )
    cleaned = _TEXT_RE.sub("", cleaned)
    removed = removed_comments + removed_texts
    if removed:
        log.info("citymap.chrome stripped %d caption(s) via fallback", len(removed))
    return cleaned, removed
