# Test fixtures

Both samples are real user-supplied meshes for the STL artwork generator. The
generated SVGs are artwork-only (geometry paths, no `<text>` and no `<image>`)
and are inputs to `POST /v1/compose-sheet` with `artwork: "inline"` — never
gallery sheets of their own.

The generator uses `slice3d` cross-sections along the mesh's **thinnest** axis
and projects each section into the (longest, second-longest) plane. That is the
user-confirmed third projection: a broad-face view with length horizontal and
width vertical. Regenerate with the user Python (`slice3d` and `trimesh[easy]`
are intentionally not in `backend/uv.lock`):

```bash
python3 -m backend.penplot.stl2plot backend/tests/fixtures/<name>.stl \
  -o backend/tests/fixtures/<name>.svg --slices 8
```

## `knipexXs.stl`

ASCII STL of a Knipex XS pliers model, copied from `~/Downloads/knipexXs.stl`
on 2026-11-24. 2306 faces, extents `[33.43, 103.53, 10.0]`. The generated
`knipexXs.svg` has 16 plot paths.

Licence: supplied by the project user with no explicit redistribution licence.
The user asked to add the resulting drawing to the shop's `tools` gallery;
rights to the source mesh and to the product copy still need an owner decision
before sale.

## `crescent-wrench-pair.stl`

Binary STL of one wrench from the user-supplied archive
`~/Downloads/crescent-wrench-pair20180403-25492-5s5yc6.zip` (2026-11-24). The
archive contains `wrench_v2_assembly_FIXED.stl` and
`wrench_v2_assembly_VORONOI_FIXED.stl`; the user chose the plain **FIXED** mesh,
which is copied here under a short name. 65,502 faces, extents
`[71.55, 25.27, 265.98]`. The generated `crescent-wrench-pair.svg` has 37 plot
paths. The uncompressed archive and the Voronoi variant are not kept in git.

Licence: supplied by the project user with no explicit redistribution licence.
Confirm rights before publishing the drawing as gallery artwork.
