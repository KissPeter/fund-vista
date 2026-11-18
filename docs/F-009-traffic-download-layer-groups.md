- Status: backend done, shop pending
- Created: 2026-11-18
- Last updated: 2026-11-18

# F-009 Finished traffic SVG keeps its layer groups

"Kész SVG letöltése" downloads the `/v1/convert` result, which used to merge
every traffic layer into one `<g>` (thousands of loose paths).

## Backend (this change)

`ConvertParams.layer_groups` (default `false`, vector contour inputs only,
runway split wins): each `<g id="traffic-*">` of the draft stays its own
`<g id="traffic-<layer>" inkscape:groupmode="layer" inkscape:label="...">` in
the result; empty layers are dropped, page furniture stays in the first group,
geometry is registered with the flat sheet. Strokes stay black.

## Follow-up (deferred)

- The shop's Traffic tab MUST send `layer_groups: true` on its convert call, so
  the download carries the layers. Repo: `pen-pixel-shop`
  (`src/components/custom/TrafficTab.tsx`, `src/lib/penplot.api.ts`).
- Owner: Péter Kiss (peter.kiss@pepperstone.com).
