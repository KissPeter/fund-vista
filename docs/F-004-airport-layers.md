# F-004 — Airport layer toggles (tick any)

- Status: in progress
- Created: 2026-09-14
- Last updated: 2026-09-14
- Type: feature
- Scope: `backend/airports` only (context query/split, `layers` param
  through schemas/render/router, page checkboxes, tests). F-001–F-003
  behavior preserved when layers are omitted.

## Motivation

One fixed drawing fits nobody: the plotter wants airfield-only, the
poster wants streets + water around it. Same picker pattern as
`/citymap` ("layers (tick any)"), adapted to airfields.

## Layers (14)

- Airfield (always fetched, `around` aeroway query): `runway`,
  `taxiway`, `apron`, `terminal`, `hangar`, `stands`, `stopways`.
- Context (second `around` query, only fired when one is selected):
  `highways`, `roads`, `paths`, `rails`, `waterway`, `water`,
  `buildings` — same tag split as citymap.

Defaults (param omitted): the 7 airfield layers — today's look, no
extra Overpass load. `layers: []` → 422; unknown id → 422
(`extra="forbid"` conventions). Replaces the F-002 `context: bool`
(one flag cannot express 7 layers; removed).

## Semantics

- Draw, count and fit only selected layers (fit follows what's drawn —
  unticking runways reframes on the rest).
- Context group stays faintest under the airfield; per-class
  `context-<id>` groups so tests/clients can tell them apart.
- `path_counts` keys: 7 airfield + 7 context ids (no `context`
  aggregate). SVG cache key includes the sorted layer set.
- Labels (badges/compass/strip-free) always draw — they annotate, they
  are not layers.

## QA / DoD

- Unit (no network): context tag split per class; render with
  `layers=["taxiway"]` draws/counts/fits only it; unknown/empty → 422;
  defaults == airfield-only (no context fetch attempted).
- Live (manual, network, 2026-09-14): LHBP all-layers render (r=3000,
  min-detail 5) → 9080 paths — runways 9, taxiways 116, roads 1527,
  highways 105, rails 28, waterway 40 + water 5, buildings 6811;
  airfield-only → 261 paths, context all zero; omitted `layers` is
  byte-identical to the explicit airfield set (46435 bytes).
  (Measured with the 3-line runway treatment; the later solid-band
  treatment draws 9 lines/strip, so LHBP runway counts are now 21
  all-layers / airfield-only and totals shift +12.)
- Min-detail fix: area outlines (runway/apron/terminal/hangar/stands/
  stopways/water/buildings) filter by footprint longest-side, open lines
  still by path length — a 12×12 m shed (48 m perimeter) survived 30 m
  before, now drops. Live LHBP: buildings 6811 → 517, total 9080 → 2393
  at min-detail 30.
- Full suite green (202 passed); commit `feat(airports): …`.
