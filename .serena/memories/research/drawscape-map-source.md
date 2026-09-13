# Drawscape map source (2026-09-13)

Drawscape `POST /api/map-render` uses `tileset: mapbox.mapbox-streets-v8` with layers `[highways,roads,rails,water,waterway,buildings,aeroway,ferry]` for Zalaegerszeg bbox zoom 12, re-rendered white-on-blue (#143e7f) preview 246x378 webp.

Mapping: Highways/Roads/Rails/Ferry -> Mapbox `road` classes; Water -> `water`; Rivers -> `waterway`; Buildings -> `building`; Airports -> `aeroway`.

Ultimate source for HU: OpenStreetMap replication feed (Mapbox v8 = OSM + Microsoft buildings + proprietary admin/ocean). Free replicate via Overpass/Geofabrik/OSMnx.

Mapbox v8 is NOT free: token required, Vector Tiles ~200k req/mo free then ~$0.25/1k; Web SDK ~50k loads free; Mobile ~25k MAUs free. Attribution required.

Full doc: `docs/drawscape-map-source-findings.md`. Skill: `.opencode/skills/document-findings/SKILL.md`.
