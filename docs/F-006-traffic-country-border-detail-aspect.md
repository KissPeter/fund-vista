# F-006 — Traffic map: country border, paper-fill aspect, Detail (min. path length)

- Status: done
- Created: 2026-10-27
- Last updated: 2026-10-27
- Type: feature
- Decided: the traffic render (backend r4) gains a Hungary admin-boundary
  basemap layer riding the Map switch, a viewport-aspect framing so the
  SVG canvas matches the sheet's A4 shape (the view grows to fill, clamped
  to the loaded day bbox), and a `min_path_len_m` detail filter (the
  city-roads minLength fast path). The shop's traffic tab drives both new
  knobs (aspect by the live sheet orientation, a 0–100 m Detail slider
  defaulting to 10 m).

## Motivation

Three user-reported gaps on the shop's `/en/custom-design/traffic` tab:

1. **No country context.** The Map layer draws motorways / waterways /
   water, but the country outline — the one thing that makes a country
   view readable — is missing.
2. **Busy days plot forever.** Every clipped polyline piece ships to the
   plotter, including sub-detail stubs; the citymap designer already has
   the fix ("Detail (min. path length)", city-roads' `minLength`), the
   traffic tab did not.
3. **The paper is not filled.** Radius views render square, the country
   view renders the day bbox (~1.28 w/h); convert scales to fit
   (`layout_scale` = min ratio), so a mismatched SVG leaves one axis of
   the A4 sheet empty.

## Scope

### Backend (fund-vista, render version r5)

- `backend/traffic/basemap.py` — the `country` layer: Overpass union of
  Hungary's `admin_level=2` relation by `ISO3166-1=HU` and by
  `name=Magyarország` (either tag path survives; the union dedupes),
  `out geom` member ways → polylines, L3-cached per quantised view,
  degrade-to-empty + `basemap_unavailable` like every layer.
- `backend/traffic/render.py`
  - `PENS["country"]` (`Country border`, plotted first — under
    everything) in `MAP_LAYERS`, so the existing **Map** switch controls
    it end to end (fetch skip when unticked, group drop, L3/L4 keys).
  - `Projection(bbox, width, aspect)` — with an aspect the canvas is
    exactly `width × width/aspect` and the bbox content is fit-contain
    centred (never distorted); without one the historical bbox-ratio
    canvas is kept.
  - `expand_view_to_aspect(view, aspect, limits)` — grows one axis around
    the centre to the target ratio, clamped to the loaded day bbox (the
    ingest clipped the traces there, so beyond it there is nothing to
    draw); never shrinks a picked view; the residual mismatch letterboxes
    in the SVG.
  - `min_path_len_m` — after clip, pieces shorter than the knob (meters
    resolved through the projection scale, so it means the same at every
    zoom and width) are dropped on **every** layer, basemap and movements
    alike.
  - `RENDER_VERSION` r4 → r5 (corridor collapse, landed mid-flight, took
    r4; cached r4 SVGs have no border and a bbox-ratio canvas, unfiltered
    stubs — they must expire).
- `backend/traffic/schemas.py` — `RenderRequest.min_path_len_m`
  (0..100, default 0 = off) and `RenderRequest.viewport_aspect`
  (0 < a ≤ 10, default None = historical framing); both optional so old
  bodies (cart render-params replays) validate unchanged.
- `backend/traffic/router.py` — the view grows before the containment
  check; the belt-and-braces basemap fallback covers the new layer.
- `backend/traffic/render_cache.py` — both knobs join the L4
  fingerprint.

Fulfillment needs no change: `traffic_group_ink` sends unknown group
suffixes (`traffic-country`) to the Pen ink.

### Shop (pen-pixel-shop)

- `src/lib/traffic.params.ts` — `TrafficRenderInput.minPathLen` /
  `.aspect` → `min_path_len_m` / `viewport_aspect`, conditional on the
  wire (a body without them stays byte-identical to the pre-r4 shape, so
  committed cart records replay unchanged); client-side guards mirror the
  backend bounds (throw, never a 422 round-trip).
- `src/components/custom/TrafficTab.tsx` — the Detail state (default
  10 m, the map designer's default), snapshot/staleness wiring, render +
  import bodies; the aspect is read at fire time from the live sheet
  orientation (`pageAspectRatio`); an orientation flip re-renders
  (geometry, not style) so the art keeps matching the sheet; Reset
  restores 10 m.
- `src/components/custom/TrafficControls.tsx` — the Detail slider in the
  Layers flyout (0–100 m, step 1, same label key as the map designer).
- `src/i18n/messages_custom.ts` — the HU/EN hint line for the slider.

## Tests

- Backend (pure + live-HTTP, all hermetic): country layer rides the Map
  switch (+ the endpoint fetch tuple), aspect framing/centring (landscape
  letterboxes left/right, portrait top/bottom, no aspect = the old
  canvas), min-length EPs (short pieces vanish on basemap AND movements,
  0 = old behaviour), `expand_view_to_aspect` (grow the demanded axis,
  clamp at the day bbox, never shrink, matching ratio untouched), schema
  bounds, cache fingerprint, RENDER_VERSION bump.
- Shop: unit (body mapping, byte-identical omission, client guards) +
  hermetic e2e (`TC-D41` extended in both locales: the aspect + the
  detail default ride every body; sliding the knob re-renders and
  re-signs).

## DoD

- [x] Settings-only env (no new env; the knobs are request parameters)
- [x] Pydantic v2 request models (`extra="forbid"` kept)
- [x] TDD: new backend tests red before the fix (the r4 behaviours did
      not exist), green after
- [x] Test types: pure unit for the renderer math, live-server HTTP for
      the endpoint tuple
- [x] Negative coverage: malformed aspect / min-length bounds → 422
- [x] Full backend suite green (traffic files; the full suite runs in
      the docker dev image — see the commit message)
- [x] Docs + Serena memory synced (this file; the roadmap memory)
- [x] Commit per phase (backend commit, shop commit)
- [x] Push only when green
