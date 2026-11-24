# Penplot SVG / STL toolchain

Date: 2026-11-24. Full findings: `docs/stl-svg-generation-toolchain.md`; CLI docs: `docs/stl2plot-cli.md`.

## Decision
- STL -> artwork-only SVG uses `slice3d` cross-sections and emits ONE view: the third orthographic projection (broad-face / top), NOT end-on or edge-on. Both earlier views were rejected by the user.
- Hand-rolled hidden-line removal and the three-view layout were removed.
- `svg3d` was evaluated but rejected (shaded triangles, not single-stroke line art).

## External tools (user Python 3.13, NOT `backend/uv.lock`)
- `slice3d 0.1.1`: `load_mesh`, `slice_mesh(axis, thickness)`, `Slice.is_empty`, `Slice.polylines_3d`, `save_section`.
- `trimesh 5.2.2` + `trimesh[easy]` soft deps: mesh parsing, extents, sections.
- Installed with `python3 -m pip install --user slice3d 'trimesh[easy]'`; scripts in `~/Library/Python/3.13/bin` are not on PATH.
- `backend/.venv` does NOT have these; run the CLI with `python3`. Tests use `pytest.importorskip` and skip there.
- Do not touch `backend/uv.lock` without regenerating the shop licence inventory (`mem:ops/shop-licence-inventory-coupling`).

## Existing SVG code to reuse, never rebuild
- `backend/penplot/stages/layout_svg.py`: `to_svg(lines_mm, w, h, stroke_mm=0.2)` -> `fill="none"` paths, `mm` dimensions, no `<text>`/`<image>`.
- `backend/penplot/stages/simplify.py`: `linesimplify(lines, tol)` (RDP).
- `backend/penplot/svg_vector.py`: `parse_svg_vectors(bytes)` for tests.
- `backend/penplot/compose_sheet.py`: `compose_sheet_svg(body, *, max_svg_bytes)` is keyword-only and pure.
- `backend.penplot.schemas.ComposeSheetRequest.artwork` defaults to `"image"`; gallery artwork MUST pass `"inline"`.
- AGENTS.md "One sheet source": `POST /v1/compose-sheet` is the only composer of preview/personalisation/order sheets.

## Axis mapping (third / broad-face view)
- `thin, mid, long = argsort(mesh.extents)`.
- Slice along `thin`; project `polylines_3d[:, [long, mid]]` so length is horizontal and width vertical.
- Scale longest extent to `--fit-mm` (default 180), add `--margin-mm` (5), simplify at 0.06 mm, emit via `to_svg`.
- Deterministic output: drop duplicate consecutive points, normalise polyline direction, sort polylines.
- `slice3d` gives cross-sections, not orthographic hidden-line removal; that trade-off is accepted.

## Samples
- `backend/tests/fixtures/knipexXs.stl`: 2306 faces, not watertight, extents [33.43, 103.53, 10.0]; generated broad-face `knipexXs.svg` has 16 paths and no text/image. User supplied it with no explicit redistribution licence but asked to add it under the shop `tools` gallery category.
- Crescent Wrench Pair: zip `crescent-wrench-pair20180403-25492-5s5yc6.zip` found in `~/Downloads` on 2026-11-24; contains `wrench_v2_assembly_FIXED.stl` (65,502 faces) and `wrench_v2_assembly_VORONOI_FIXED.stl` (215,318 faces). User chose plain FIXED; committed as `backend/tests/fixtures/crescent-wrench-pair.stl` + `.svg` (37 paths, extents [71.55, 25.27, 265.98], thin=Y, mid=X, long=Z). Voronoi variant and zip not kept in git. User confirmed the third (broad-face) projection is the right one.

## Shop gallery item (in progress)
- Worktree `/Users/kissp/PycharmProjects/pen-pixel-shop-knipex`, branch `feat/knipex-gallery-item` from `origin/main`.
- Seeds: `src/lib/tools.seeds.ts` (`TOOLS_SEEDS`, `PlaneSeed`); asset: `public/artwork/tools/<slug>.svg` (A4 landscape 297x210, plotter-safe, no text/image/style, metadata stamped).
- Source rules: `docs/tools-sources.md`, `docs/new-source-rule.md` (5-point checklist), `docs/terms-requirements.md` chapter 3 ToS row, `specTable` "Forrásfájl" row stays.
- Gate: `npm run test:unit -- src/lib/shop.server.test.ts src/lib/gallery-routes.test.ts src/lib/galleryArtworkAssets.test.ts`.
- Sheet for the shop asset MUST come from `compose_sheet_svg(..., max_svg_bytes=...)` with `artwork="inline"` (One sheet source); stamp ownership metadata with `svgmeta.stamp`.
- DONE: two gallery items on branch `feat/knipex-gallery-item` (`ba888bb0`, `66876aa3`): `p-tools-knipex-xs-fogo` / `knipex-xs-fogo.svg` and `p-tools-crescent-wrench-pair` / `crescent-wrench-pair.svg`, both directly under `tools` (no subcategory). Source logs updated, ToS row S15 + `terms.src.tools.*` HU/EN keys + TermsPage section added. Rights still flagged open before sale (no SPDX on the STLs). Shop unit gate: 35 passed.

## Reproduce
`python3 -m backend.penplot.stl2plot backend/tests/fixtures/knipexXs.stl -o backend/tests/fixtures/knipexXs.svg --slices 8`
`python3 -m pytest backend/tests/test_penplot_stl_views.py -q`
`rsvg-convert -b white -w 1400 backend/tests/fixtures/knipexXs.svg -o knipex-third-view.png`