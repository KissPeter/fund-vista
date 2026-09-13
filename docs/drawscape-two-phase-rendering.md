# Drawscape two-phase rendering vs fund-vista static SVG

Date: 2026-09-13
Skill: `.opencode/skills/document-findings/SKILL.md`
Sources:
- `docs/drawscape-map-source-findings.md` (captured `POST /api/map-render`
  payload: `bounds` + `zoom: 12` + `tileset: mapbox.mapbox-streets-v8` +
  `layers[]` + `size.width_mm/height_mm` + `preview 246x378 webp`)
- `docs/hungary-europe-map-providers.md` (free tile alternative)

## The difference

Drawscape's city-map designer is **two-phase**; fund-vista `/v1/citymap`
is **single-phase**. That explains why their preview behaves like
Google/OSM (zoom reveals detail, mouse pans) while ours returns one
static SVG immediately.

## Phase 1 — interactive preview (browser, no SVG)

A slippy vector map (MapLibre GL JS style) over Mapbox Streets v8 tiles:

- World pre-cut into XYZ tiles per zoom: `/{z}/{x}/{y}.mvt` (protobuf,
  named layers `road`, `water`, `building`, `aeroway`, … + attributes
  like road `class`).
- **Zoom-dependent generalization happens at tile-build time**: z5 tiles
  carry only motorways; minor streets appear ~z10–12; buildings ~z14+.
  Zooming loads *different, denser* tiles — the client does not filter.
- **Panning** fetches neighboring tiles at the same zoom and stitches them.
- Layer checkboxes (Highways, Roads, …) are **style filters** on the
  already-loaded vector source, e.g. `road where class in
  (motorway, trunk, primary)`. Toggling is instant; no new data fetch.

## Phase 2 — final render (server, reproducible from bounds)

Ordering/rendering posts the viewport — top-left/bottom-right is enough:

```json
{"bounds": {"north": 46.56, "south": 46.51, "east": 16.78, "west": 16.74},
 "zoom": 12, "tileset": "mapbox.mapbox-streets-v8",
 "layers": ["highways", "roads", "rails", "water", "waterway", "buildings", "aeroway", "ferry"],
 "size": {"width_mm": 245.3, "height_mm": 376.9},
 "color_scheme": "blue_white", "output_format": "webp"}
```

Server: bounds → covering tile set at a print-resolution zoom → fetch
same tiles server-side → clip to bounds → project lon/lat to mm →
monochrome re-render (low-res `webp` preview first, hi-res SVG/PDF for
production). `bounds + zoom + layers` fully determines the view, so any
resolution can be reproduced later.

## What fund-vista does instead

`geocode → Overpass QL (full detail, one live query) → split → static
SVG`. No zoom generalization (we request buildings at "z5" scale — the
cause of the triple-504 outages), no pan/zoom preview. Simpler, but one
heavy live query per render, mitigated by Redis cache + OSM Main API
fallback (`backend/citymap/osm_api.py`).

## Reproduce the Drawscape architecture free

- Tiles: OpenFreeMap `https://tiles.openfreemap.org/planet/latest/{z}/{x}/{y}.pbf`
  (OpenMapTiles schema, no key) — same XYZ/MVT mechanics as Mapbox.
- Preview: client-side MapLibre with style filters per our layer ids
  (`transportation.class` → highways/roads/paths/rails/ferry; `water`,
  `waterway`, `building`, `aeroway` direct). Zero backend needed.
- Final: backend fetches covering tiles, clips to bounds, runs existing
  `render_svg` — only phase 2 touches the server.
