"""The pure ink-split: one SVG per distinct ink from a layer-grouped draft.

The renderers emit one top-level ``<g id="...">`` group per layer
(``backend/traffic/render.py``: ``traffic-<layer>``; ``backend/airports/render.py``:
``runways`` / ``runway-marks`` / ``osm-runways`` / ``context-*`` / ``osm-*``;
``backend/citymap/render.py``: ``citymap-<layer>``). The split is **pure group
filtering by documented group ids** — no renderer changes (shop #1079):

- basemap / frame / title block / label groups stay on the **Pen ink**;
- each buyer-pickable layer group goes to its chosen ink (``auto`` ⇒ Pen —
  a layer without an entry uses the Pen ink, the #1078 fulfilment rule);
- groups sharing a color merge into **one file** (one pen swap);
- empty layers (no drawn children) produce **nothing**.

The splitter is byte-preserving: kept groups are carried verbatim, only the
``stroke=`` of a carried group's open tag is re-pointed at ``ARTIFACT_STROKE``.
The ink lives in the pass's **name** only (``ink``, the file name the shop
stores): every pass file is drawn in black, whatever its ink, so a white-ink
pass on blue/black paper is not invisible on a white screen or printout. The
owner swaps the pen per file; the plotter takes the pen from the file, not the
stroke color (``INK_HEX`` mirrors the shop's ``RUNWAY_INK_HEX`` palette and
names the valid inks; it is no longer written into the files).
Unknown group ids default to the Pen ink, so a renderer that grows a new
layer can never strand ink outside a pass.

Parsing is a depth-counting scanner over the machine-generated drafts, not a
DOM round-trip: kept content stays byte-identical (namespace prefixes and
attribute order included), which keeps the per-pass files diff-stable and
deterministic.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

#: Plotter ink hexes — the shop's ``RUNWAY_INK_HEX`` (issue #938) plus white
#: (electric-blue paper's single ink). The keys are the valid ink names; the
#: hexes are the buyer-facing display palette and are NOT written into the
#: pass files (see ``ARTIFACT_STROKE``).
INK_HEX: dict[str, str] = {
    "black": "#1a1a1a",
    "red": "#c0392b",
    "blue": "#2456a6",
    "green": "#1e7a46",
    "white": "#ffffff",
}

#: The stroke every pass file is drawn in, whatever its ink. Only the file name
#: carries the pen color (owner decision): white-pen files would otherwise be
#: invisible when opened on a white background.
ARTIFACT_STROKE = "#000000"

#: Airport draft groups that belong to the runway layer — the shop's
#: ``RUNWAY_GROUP_IDS`` (``src/lib/airportSvgTint.ts``, "live backend shape";
#: ``runway`` is a legacy alias kept for old drafts). The runway labels
#: (``runway-marks``) are text, not the runway layer: the buyer's preview draws
#: them in the Pen ink, so they stay on the Pen file.
AIRPORT_RUNWAY_GROUPS: frozenset[str] = frozenset(
    {"runways", "osm-runways", "runway"}
)

#: Traffic draft group suffixes that always ride the Pen ink: the Map layer
#: groups (motorways / waterways / water), the Airport layer's airfield group
#: and the Phase-5 category pen layers (the #1078 selectors cover the four
#: movement layers only; a category-mode design has no per-layer inks).
TRAFFIC_PEN_SUFFIXES: frozenset[str] = frozenset(
    {"motorways", "waterways", "water", "airfield", "wide", "narrow", "regional", "other"}
)

#: Shop wire layer id (``src/lib/trafficInk.ts`` ``TRAFFIC_LAYER_IDS``) → the
#: draft's group-id suffix (``backend/traffic/render.py`` ``PENS`` keys).
TRAFFIC_WIRE_TO_SUFFIX: dict[str, str] = {
    "arrivals": "arrivals",
    "departures": "departures",
    "holding": "holding",
    "goAround": "go_around",
}


@dataclass(frozen=True)
class InkFile:
    """One pen pass: the ink name, its hex and the carried group ids."""

    ink: str
    layers: list[str] = field(default_factory=list)
    svg: str = ""

    @property
    def hex(self) -> str:
        """The pass's stroke color: always ``ARTIFACT_STROKE`` (the ink is
        the name); empty for an unknown ink = unstyled."""
        return ARTIFACT_STROKE if self.ink in INK_HEX else ""


def airport_group_ink(group_id: str, pen_ink: str, runway_ink: str | None) -> str:
    """Airport mapping: runway groups → runway ink, everything else → Pen.

    A missing runway choice (``None``) means "auto": the runway rides the
    Pen ink and the design is a single-pen plot (the #923 default).
    """
    effective = runway_ink if runway_ink else pen_ink
    if group_id in AIRPORT_RUNWAY_GROUPS:
        return effective
    return pen_ink


def traffic_group_ink(
    group_id: str, pen_ink: str, layer_inks: dict[str, str] | None
) -> str:
    """Traffic mapping: ``traffic-<suffix>`` → the buyer's layer ink or Pen.

    ``layer_inks`` uses the shop's wire layer ids (``arrivals`` / ``departures``
    / ``holding`` / ``goAround`` — ``goAround`` maps to the draft's
    ``go_around`` suffix). A suffix that is not a buyer-pickable layer
    (basemap, airfield, category pens, anything unknown) rides the Pen ink.
    """
    suffix = group_id[len("traffic-"):] if group_id.startswith("traffic-") else ""
    if not suffix or suffix not in TRAFFIC_WIRE_TO_SUFFIX.values():
        return pen_ink
    if not layer_inks:
        return pen_ink
    for wire_id, wire_suffix in TRAFFIC_WIRE_TO_SUFFIX.items():
        if wire_suffix == suffix:
            chosen = layer_inks.get(wire_id)
            return chosen if chosen else pen_ink
    return pen_ink


def _find_top_level_groups(svg_text: str) -> tuple[str, list[tuple[str, str | None]]]:
    """Split a draft into ``(root_open_tag, [(open_tag, body_or_None), ...])``.

    Returns the ``<svg ...>`` open tag and the top-level chunks in source
    order: a chunk is a ``(<g ...>, body)`` pair for a group (``body`` is the
    text between the open tag and its matching ``</g>``) or a ``(element,
    None)`` pair for any other top-level element. Text between elements
    (whitespace) is dropped — the writer re-joins with newlines, matching
    the renderers' ``"\\n".join`` output shape.
    """
    root_start = svg_text.find("<svg")
    if root_start < 0:
        raise ValueError("no <svg> root element")
    root_end = svg_text.find(">", root_start)
    if root_end < 0:
        raise ValueError("unterminated <svg> root tag")
    root = svg_text[root_start:root_end + 1]

    chunks: list[tuple[str, str | None]] = []
    i = root_end + 1
    n = len(svg_text)
    while i < n:
        lt = svg_text.find("<", i)
        if lt < 0:
            break
        if svg_text.startswith("</svg", lt):
            break
        if svg_text.startswith("<g", lt) and svg_text[lt + 2] in (" ", ">", "\n"):
            # Walk nested groups: a group opens on "<g" and closes on the
            # matching "</g>" (depth-counted, string-safe).
            depth = 1
            j = lt + 2
            open_end = -1
            while j < n:
                if svg_text.startswith("<g", j) and svg_text[j + 2] in (" ", ">", "\n"):
                    depth += 1
                    j += 2
                elif svg_text.startswith("</g>", j):
                    depth -= 1
                    j += 4
                    if depth == 0:
                        break
                else:
                    if open_end < 0 and svg_text[j] == ">":
                        open_end = j
                    j += 1
            else:
                raise ValueError("unterminated <g> group")
            if open_end < 0:
                raise ValueError("unterminated <g> open tag")
            open_tag = svg_text[lt:open_end + 1]
            chunks.append((open_tag, svg_text[open_end + 1:j - 4]))
            i = j
        else:
            gt = svg_text.find(">", lt)
            if gt < 0:
                raise ValueError(f"unterminated element at offset {lt}")
            chunks.append((svg_text[lt:gt + 1], None))
            i = gt + 1
    return root, chunks


def _group_id(open_tag: str) -> str:
    """The ``id=`` of a group open tag; empty when absent."""
    marker = 'id="'
    at = open_tag.find(marker)
    if at < 0:
        return ""
    end = open_tag.find('"', at + len(marker))
    return open_tag[at + len(marker):end] if end >= 0 else ""


def _restroke(open_tag: str, hex_color: str) -> str:
    """Point a group open tag's ``stroke=`` at the pass's stroke hex
    (``ARTIFACT_STROKE``: black for every ink).

    Every renderer group carries ``stroke="..."``; a group without one gets
    the attribute injected right after ``<g`` so the pass stays internally
    consistent (the plotter reads one file per pen, named by its ink).
    """
    marker = 'stroke="'
    at = open_tag.find(marker)
    if at >= 0:
        end = open_tag.find('"', at + len(marker))
        if end >= 0:
            return open_tag[:at] + f'stroke="{hex_color}"' + open_tag[end + 1:]
    return open_tag.replace("<g", f'<g stroke="{hex_color}"', 1)


def _has_ink(body: str) -> bool:
    """True when a group body would leave ink on the page (any element)."""
    return "<" in body


def split_by_ink(svg_text: str, group_ink) -> list[InkFile]:
    """Split a layer-grouped draft into one SVG per distinct ink.

    ``group_ink`` maps a group id to an ink name (``INK_HEX`` key); the
    callable receives every top-level group id. Groups (and any non-group
    top-level element, e.g. chrome a renderer emitted outside a group) land
    in the file of their ink, in source order; empty groups are dropped; an
    ink whose groups were all empty produces no file. The root open tag is
    carried verbatim. Deterministic: same draft + same mapping ⇒ same bytes.
    """
    root, chunks = _find_top_level_groups(svg_text)

    # ink → list of (open_tag, body) / (element, None) chunks, source order.
    passes: dict[str, list[tuple[str, str | None]]] = {}
    order: list[str] = []
    for open_tag, maybe_body in chunks:
        if maybe_body is not None and not _has_ink(maybe_body):
            continue  # empty layer: produces nothing
        gid = _group_id(open_tag) if maybe_body is not None else ""
        ink = group_ink(gid) if maybe_body is not None else _default_ink(group_ink)
        if ink not in passes:
            passes[ink] = []
            order.append(ink)
        passes[ink].append((open_tag, maybe_body))

    files: list[InkFile] = []
    for ink in order:
        hex_color = ARTIFACT_STROKE  # the ink is the file name, never the stroke
        parts: list[str] = [root]
        layers: list[str] = []
        for open_tag, maybe_body in passes[ink]:
            if maybe_body is not None:
                gid = _group_id(open_tag)
                if gid:
                    layers.append(gid)
                parts.append(_restroke(open_tag, hex_color))
                parts.append(maybe_body)
                parts.append("</g>")
            else:
                parts.append(open_tag)
        parts.append("</svg>")
        files.append(InkFile(ink=ink, layers=layers, svg="\n".join(parts) + "\n"))
    return files


def _default_ink(group_ink):
    """The Pen ink for a non-group element: probe the mapper with a sentinel
    every mapping answers with the Pen (unknown ids default to Pen)."""
    return group_ink("")


def design_id_for(svg_text: str) -> str:
    """The content-addressed design id of a draft (sha256 hex, lowercase).

    ``design_id`` reuses the penplot ``image_id`` — the sha256 of the exact
    bytes (``backend/penplot/tokens.py``). Fulfillment re-renders from the
    persisted params and checks this digest before splitting, so a stale
    cache or a renderer-version bump fails loudly instead of plotting the
    wrong design.
    """
    return hashlib.sha256(svg_text.encode("utf-8")).hexdigest()
