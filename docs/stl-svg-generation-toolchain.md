# STL -> SVG toolchain findings (2026-11-24)

## Source and status

- Date: 2026-11-24.
- Sources: local package inspection with `python3` (`slice3d`, `trimesh`,
  `svg3d`), the [svg3d documentation](https://svg3d.readthedocs.io/en/latest/),
  and existing repo code under `backend/penplot/`.
- Decision: use `slice3d` cross-sections and emit **side view only**. The
  earlier hand-rolled hidden-line implementation and the three-view layout
  were removed. `svg3d` was evaluated and rejected (shaded triangle rendering,
  not single-stroke line art).

## Installed external tools

Installed globally with `python3 -m pip install --user`; **not** added to
`backend/uv.lock` (see `docs/CR-008-shop-licence-inventory-coupling.md`).

| Tool | Version / detail | Role |
|------|------------------|------|
| `slice3d` | 0.1.1 | `load_mesh`, `slice_mesh`, section polylines |
| `trimesh` | 5.2.2 | mesh parsing, extents, sections |
| `trimesh[easy]` | soft deps (`scipy`, `shapely`, `networkx`, ...) | clean binary installs on Linux x86_64, macOS ARM, Windows x86_64 |
| `svg3d` | 0.2.1 | evaluated only; not in the final path |
| `numpy`, `cv2`, `PIL` | already available | supporting math / preview work |
| `rsvg-convert` | system tool | raster preview of generated SVG |

Scripts installed under `~/Library/Python/3.13/bin` are not on `PATH`; call the
Python API or `python3 -m backend.penplot.stl2plot` instead.

## SVG generation capability already in the repo

Reuse these; do not rebuild them:

| Path | Capability |
|------|------------|
| `backend/penplot/stages/layout_svg.py` | `to_svg(lines_mm, page_w, page_h, stroke_mm=0.2, ...)` emits `<path fill="none">` geometry with `mm` width/height/viewBox; no sheet chrome |
| `backend/penplot/stages/simplify.py` | `linesimplify(lines, tol)` RDP polyline simplification |
| `backend/penplot/svg_vector.py` | `parse_svg_vectors(bytes)` for path parsing in tests |
| `backend/penplot/compose_sheet.py` | `compose_sheet_svg(body, *, max_svg_bytes)` — keyword-only, pure A4 sheet composer |
| `backend/penplot/schemas.py` | `ComposeSheetRequest.artwork` defaults to `"image"`; gallery artwork MUST pass `"inline"` |
| `backend/tests/fixtures/aichi-m6a1-seiran-3-view-line-drawing.svg` | existing line-art reference fixture |

AGENTS.md "One sheet source": `POST /v1/compose-sheet` MUST remain the only
composer of preview, personalisation and order sheets. Generated artwork MUST
NOT contain `<text>` or `<image>`.

## `slice3d` API and axis mapping

- `slice3d.load_mesh(path)` -> `trimesh.Trimesh`.
- `slice3d.slice_mesh(mesh, axis="x|y|z", thickness=float)` yields `Slice`
  objects with `is_empty`, `polylines_3d` and `path_2d`.
- `slice3d.save_section(path_2d, out.svg)` exports raw section SVG; the CLI
  uses `polylines_3d` and the repo's `to_svg` so output stays artwork-only.
- Axes are ranked by extent: `thin, mid, long = argsort(mesh.extents)`.
- **Third (broad-face / top) view mapping:** slice along `thin`; project each
  polyline's `[long, mid]` columns into the drawing plane, so length is
  horizontal and width vertical. Scale the longest extent to `--fit-mm`,
  add `--margin-mm`, simplify at 0.06 mm, emit with `to_svg`. The first two
  projections (end-on and edge-on) were rejected by the user.
- Output is deterministic: duplicate consecutive points are dropped, open
  polylines are direction-normalised and polylines are sorted.

`slice3d` produces cross-sections, not orthographic hidden-line removal. That
is the accepted trade-off for this gallery artwork.

## Sample facts and reproduction

`backend/tests/fixtures/knipexXs.stl`: 2306 faces, not watertight, bounds
`[[-19.05, -53.12, 0.0], [14.38, 50.41, 10.0]]`, extents
`[33.43, 103.53, 10.0]`. With 5 mm slices, nonempty counts were x 7/7,
y 21/21, z 3/2. The generated broad-face (third) view has 16 plot paths and no text/image.

The mesh was supplied by the user with no explicit redistribution licence:
do not publish it as gallery artwork without confirming rights.

```bash
python3 -m pip install --user slice3d 'trimesh[easy]'
python3 -m backend.penplot.stl2plot backend/tests/fixtures/knipexXs.stl \
  -o backend/tests/fixtures/knipexXs.svg --slices 8
python3 -m pytest backend/tests/test_penplot_stl_views.py -q
rsvg-convert -b white -w 1400 backend/tests/fixtures/knipexXs.svg \
  -o knipex-side-preview.png
```

## Limits and next steps

- Run the CLI with `python3`; `backend/.venv` lacks the external tools and the
  pure tests skip there via `pytest.importorskip`.
- Do not modify `backend/uv.lock` without regenerating the shop licence
  inventory.
- Crescent Wrench Pair was requested as the next sample but was not present in
  `~/Downloads` at the last check. Recheck, copy it into
  `backend/tests/fixtures/` with provenance notes, then run the same command.
- See `docs/stl2plot-cli.md` for user-facing CLI documentation.
