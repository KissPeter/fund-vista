# F-001 — Citymap artwork rotation

- Status: done
- Created: 2026-09-13
- Last updated: 2026-09-13
- Type: feature

## Scope

Rotate citymap artwork clockwise around the area center before page
fitting, so street grids can be aligned to the page (portrait/landscape
independent).

- [x] `render_svg(..., rotation_deg=0.0)` rotates projected plane-meter
      points around the bbox center; bounds recomputed after rotation
      (nothing clipped); path lengths (hence `min_path_len_m`) unchanged.
- [x] `RenderRequest.rotation_deg` (default 0.0, ±180); out-of-range → 422.
- [x] SVG cache key includes rotation; both `render` and `import` pass it.
- [x] `/citymap` rotate slider (−180…180, step 5) sent on import;
      `citymapApi.ts` `rotationDeg` option (default 0).

## Tests

- [x] `rotation_deg=0` output identical to unrotated.
- [x] 90° on non-square extent changes dimensions, preserves path counts.
- [x] `rotation_deg=200` → 422 (hermetic).
- [x] `/citymap` contains rotation slider.
- [x] Full suite: 162 passed, 0 failed.
