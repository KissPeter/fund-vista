# Test fixtures

## `knipexXs.stl`

ASCII STL of a Knipex XS pliers model, copied from `~/Downloads/knipexXs.stl`
on 2026-11-24 as the first real sample for the STL side-view generator. It is
used by `backend/tests/test_penplot_stl_views.py` and by the examples in
`docs/stl2plot-cli.md`.

Licence: supplied by the project user; no explicit redistribution licence was
given. Do not publish this mesh as gallery artwork without confirming rights.

`knipexXs.svg` is generated side-view artwork (not a source fixture). It comes
from `slice3d` cross-sections along the mesh's longest axis and is regenerated
with the user Python, where `slice3d` and `trimesh[easy]` are installed:

```bash
python3 -m backend.penplot.stl2plot backend/tests/fixtures/knipexXs.stl \
  -o backend/tests/fixtures/knipexXs.svg --slices 8
```

It contains geometry only (`fill="none"` paths, no `<text>` and no `<image>`)
and is intended as input to `POST /v1/compose-sheet` with `artwork: "inline"`.

## Next sample: Crescent Wrench Pair

The user asked to process Crescent Wrench Pair after Knipex. It was not found
in `~/Downloads` at the last check on 2026-11-24. When available, copy the STL
here, add the same provenance/licence note, and regenerate its SVG with the
command above.
