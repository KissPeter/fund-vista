- Status: in progress
- Created: 2026-11-26
- Last updated: 2026-11-26

# F-010 One layered SVG for the WordPress-bridge handover

## Problem

The airport and traffic order artifacts reached the WordPress bridge as one
SVG **per ink** (per colour): the fulfilment ink-split wrote a file per
distinct ink and the shop uploaded each of them. The buyer bought one design,
so the bridge must get ONE file whose pen groups are named and coloured - the
plotter operator then sees the whole drawing and each pen at a glance.

## The earlier functionality (found in git history)

- `fee7897` (2026-11-08) added `composite_by_ink`: every non-empty layer in
  one SVG, each group re-stroked to its ink hex and labelled
  `<ink> <group id>` for the Inkscape layer panel. It shipped *next to* the
  per-ink files, so the bridge still got one file per colour.
- `77b6c73` replaced the composite with the one-file-per-ink contract
  (shop #1079) and the composite disappeared from the answer.
- `6508ef7` (branch `feat/wp-layered-svg`) restored it for the two custom
  design kinds: airport and traffic answer with the composite only, no
  per-ink copies.

## Backend (this change)

- `POST /v1/fulfill/ink-split` answers `kind=airport|traffic` with exactly
  one file (`ink: "composite"`): the whole design, every renderer group
  re-stroked to its pen colour (`INK_HEX`) and named
  (`inkscape:label="<ink> <group id>"`, `inkscape:groupmode="layer"`). The
  other kinds (weather, pcb) keep the one-file-per-ink contract.
- The composite is composed on the group-preserving vector path
  (`methods=["contour"]`): a raster method (hatch, flow, centerline) traces
  the artwork as pixels, flattens the renderer groups, and the one file would
  arrive as a single black pen - the colours the buyer paid for, gone.
  The airport and traffic designer tabs are line art (the contour preset),
  so this only ever drops a method that could not carry the groups anyway.
- The public lock-down (PR #229) is untouched: on a public instance the
  endpoint answers `404 svg_locked`; the handover runs on the internal
  instance (`PENPLOT_ALLOW_SVG=1`), which is the only place the vector bytes
  leave the backend.

## Follow-up (deferred - each with an owner)

- Shop: `toA4Sheet` must leave an already-A4 composite alone; its wrapper
  group nests the named pen layers under one anonymous group in Inkscape, so
  the pen colours are there but the layers are not readable. Repo
  `pen-pixel-shop` (branch `feat/wp-bridge-layered-svg`).
  Owner: Péter Kiss (peter.kiss@pepperstone.com).
- Shop / WP bridge: the order metabox lists the expected per-ink names (pen,
  runway, every layer colour) as "missing" when the line carries the one
  composite file. Repo `pen-pixel-shop`
  (`wp-bridge/penpixel-bridge.php`, `penpixel_order_line_inks`).
  Owner: Péter Kiss (peter.kiss@pepperstone.com).
- Deploy: the internal instance must run with `PENPLOT_ALLOW_SVG=1` and the
  shop's `PENPLOT_INTERNAL_API_BASE` must point at it; the public instance
  stays locked. Owner: Péter Kiss (peter.kiss@pepperstone.com).

## Definition of Done

- [x] The composite handover is restored for the airport and traffic kinds
      (one file, pen colours on named groups).
- [x] The composite keeps the pen colours on a raster convert (the
      group-preserving vector path is forced).
- [x] TDD: the two regression tests fail before the fix and pass after it.
- [x] Negative coverage kept: a public instance still answers `svg_locked`.
- [x] Full backend suite green (1674 passed, 45 skipped).
- [x] Docs updated (`docs/index.md` row, this file) and the Serena memory
      synced.
- [ ] Pushed and merged to `main` (the push deploys the Mac instance).
