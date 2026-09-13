"""Selectable map layers and their OpenStreetMap tag filters.

Layer ids mirror the Drawscape/Mapbox-Streets-v8 set the project already
uses (``highways, roads, rails, water, waterway, buildings, aeroway,
ferry``) plus ``paths`` (foot/cycle ways, which city-roads folds into its
default road query). Each layer declares:

- ``label`` / ``description`` — shown in layer pickers;
- ``way_selectors`` — Overpass ``way[...]`` snippets with a ``{bbox}``
  placeholder (``south,west,north,east``);
- ``relation_selectors`` — Overpass ``relation[...]`` snippets whose member
  ways are drawn into the layer (ferry/rail routes, water/building
  multipolygons).

A way matching several layers is drawn into exactly one: the first match
in ``LAYER_ORDER`` wins (see :mod:`backend.citymap.overpass`), so combined
layers never double-ink the same street.
"""

from __future__ import annotations

# Draw order / first-match priority. Highways above minor roads, route
# relations (ferry/rail) are resolved from membership first (see overpass).
LAYER_ORDER: tuple[str, ...] = (
    "highways",
    "roads",
    "paths",
    "rails",
    "aeroway",
    "waterway",
    "water",
    "buildings",
    "ferry",
)

LAYERS: dict[str, dict[str, object]] = {
    "highways": {
        "label": "Highways",
        "description": "Motorways, trunks and primary roads (incl. links).",
        "way_selectors": [
            'way["highway"~"^(motorway|trunk|primary)(_link)?$"]({bbox})',
        ],
        "relation_selectors": [],
    },
    "roads": {
        "label": "Roads",
        "description": "Minor vehicular streets: secondary/tertiary, residential, service, track.",
        "way_selectors": [
            'way["highway"~"^(secondary|tertiary|unclassified|residential|living_street|pedestrian|service|track|road)(_link)?$"]({bbox})',
        ],
        "relation_selectors": [],
    },
    "paths": {
        "label": "Paths",
        "description": "Footways, paths, cycleways, steps and bridleways.",
        "way_selectors": [
            'way["highway"~"^(footway|path|cycleway|steps|bridleway|corridor)$"]({bbox})',
        ],
        "relation_selectors": [],
    },
    "rails": {
        "label": "Rails",
        "description": "Railways, light rail, subway, tram (tracks + route relations).",
        "way_selectors": [
            'way["railway"~"^(rail|light_rail|subway|tram|narrow_gauge|monorail|preserved)$"]({bbox})',
        ],
        "relation_selectors": [
            'relation["route"~"^(train|tram|subway|light_rail)$"]({bbox})',
        ],
    },
    "aeroway": {
        "label": "Airports",
        "description": "Aerodromes, runways, taxiways, aprons, terminals.",
        "way_selectors": [
            'way["aeroway"]({bbox})',
        ],
        "relation_selectors": [],
    },
    "waterway": {
        "label": "Rivers & streams",
        "description": "Rivers, streams, canals, ditches and drains (flowing water).",
        "way_selectors": [
            'way["waterway"~"^(river|stream|canal|ditch|drain)$"]({bbox})',
        ],
        "relation_selectors": [],
    },
    "water": {
        "label": "Water",
        "description": "Lakes, reservoirs and other standing water (drawn as outlines).",
        "way_selectors": [
            'way["natural"="water"]({bbox})',
            'way["water"]({bbox})',
        ],
        "relation_selectors": [
            'relation["type"="multipolygon"]["natural"="water"]({bbox})',
        ],
    },
    "buildings": {
        "label": "Buildings",
        "description": "Building footprints (drawn as outlines).",
        "way_selectors": [
            'way["building"]({bbox})',
        ],
        "relation_selectors": [
            'relation["type"="multipolygon"]["building"]({bbox})',
        ],
    },
    "ferry": {
        "label": "Ferry routes",
        "description": "Ferry route relations (member ways).",
        "way_selectors": [],
        "relation_selectors": [
            'relation["route"="ferry"]({bbox})',
        ],
    },
}

LAYER_IDS = tuple(LAYERS)


def selectors_for(layers: list[str]) -> tuple[list[str], list[str]]:
    """Return (way selectors, relation selectors) for the requested layers."""
    ways: list[str] = []
    relations: list[str] = []
    for layer in layers:
        entry = LAYERS[layer]
        ways.extend(entry["way_selectors"])  # type: ignore[arg-type]
        relations.extend(entry["relation_selectors"])  # type: ignore[arg-type]
    return ways, relations
