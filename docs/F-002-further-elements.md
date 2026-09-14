# F-002 — Dropped aeroway elements + surrounding context

- Status: done
- Created: 2026-09-14
- Last updated: 2026-09-14
- Type: feature
- Scope: `backend/airports` only (overpass splitter, schemas, render,
  router, UI flag, tests). No changes to citymap/penplot or F-001 behavior
  when the new flag is off.

## Motivation

F-001's splitter draws 5 `aeroway` classes and silently drops the rest.
Live LHBP data shows ~140 dropped elements, most of them visually
load-bearing (120 aircraft stands), plus zero street context outside the
fence — the diagram floats on empty paper where Drawscape prints show
surrounding streets/blocks.

## Scope

1. `taxilane` → folded into the taxiway group (same centerline treatment).
2. `parking_position` → new `stands` group, thin outlines (the comb pattern
   around terminals; LHBP: 120).
3. `stopway` → new `stopways` group, thin outlines (paved overrun strips
   at runway ends; LHBP: 3).
4. Surrounding context behind `context: bool = false`: second Overpass
   `around` query for service/access roads + buildings + water, drawn
   faintest underneath everything in one `context` group. Off by default
   ( preserves the F-001 look, bounds Overpass load and plot time).
5. Still excluded (poster-scale clutter, documented): `jet_bridge`,
   `helipad`, `navigationaid`, `gate`, `parking` car parks.

## API

- `RenderRequest.context: bool = false` (`extra="forbid"` unchanged);
  threaded through render/import; context fetch is cached under its own
  key (`overpass-ctx`) and warned as `context_enabled` / `no_context_data`.
- `path_counts` gains `stands`, `stopways`, `context` keys (0 when empty).

## QA / DoD

- Unit (no network): taxilane folds to taxiway; parking_position →
  stands; stopway → stopways; jet_bridge/helipad still ignored; context
  query shape; render draws `stands`/`stopways`/`context` groups; flag
  defaults false.
- Live spot-check (manual, network): LHBP render with `context: true`
  shows stands + streets; polys parse via `parse_svg_vectors`.
- Full suite: `.venv/bin/python -m pytest backend/tests/ -q` green.
- Commit per repo convention (`feat(airports): …`).
