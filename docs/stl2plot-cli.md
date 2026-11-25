# STL -> side-view artwork CLI

`python -m backend.penplot.stl2plot` turns any binary or ASCII STL mesh into one
plot-ready SVG containing a single **side view**. The geometry comes from
`slice3d` cross-sections; the CLI only chooses the view axes, lays the
cross-sections out and emits artwork through the repo's existing `to_svg`.

Run it with the user Python (`python3`), where `slice3d` and `trimesh[easy]`
are installed. The backend virtualenv deliberately does not carry these
external tools, so `backend/uv.lock` and the shop licence inventory stay
untouched (see `docs/CR-008-shop-licence-inventory-coupling.md`).

## Install once

```bash
python3 -m pip install --user slice3d 'trimesh[easy]'
```

`svg3d` was also evaluated but is not used: it renders shaded triangle faces,
not the single-stroke cross-section line art this gallery needs.

## Usage

```bash
# single mesh -> side-view artwork
python3 -m backend.penplot.stl2plot ~/Downloads/knipexXs.stl -o knipex-side.svg

# batch: a folder becomes one SVG per STL
python3 -m backend.penplot.stl2plot models/ --out-dir svg/ --report svg/report.json
```

The sample `backend/tests/fixtures/knipexXs.stl` is the first real mesh used to
check the tool. It is not hard-coded; any STL works. The next requested sample
is Crescent Wrench Pair (not found in `~/Downloads` at the last check).

## What it emits

- **Artwork only:** paths with `fill="none"`, a pen stroke and physical `mm`
  dimensions. It MUST NOT contain `<text>` or `<image>`.
- **One view only:** `slice3d` slices along the mesh's thinnest axis; every
  section's polyline is projected into the (longest, second-longest) plane.
  This is the broad-face / top view where the plier is recognisable: length
  horizontal, width vertical. It is cross-section line art, not
  hidden-line-removal orthographic projection.
- **Canonical orientation:** axes are ranked by mesh extent. The thinnest axis
  is depth, the longest is horizontal and the second-longest is vertical.
  Polylines are direction-normalised and sorted, so facet order does not
  change bytes.
- **One sheet source:** the tool MUST NOT compose a gallery sheet. Pass the SVG
  to `POST /v1/compose-sheet` with `artwork: "inline"`; the composer owns
  preview, personalisation and order-file geometry (AGENTS.md).

```python
import httpx
from pathlib import Path

artwork = Path("knipex-side.svg").read_text()
r = httpx.post(
    "http://localhost:8000/v1/compose-sheet",
    json={"svg": artwork, "lines": ["Knipex XS"], "artwork": "inline"},
    timeout=60,
)
r.raise_for_status()
Path("gallery-sheet.svg").write_bytes(r.content)  # no <image>, no <text>
```

`compose_sheet_svg(body, max_svg_bytes=...)` is keyword-only; the HTTP route
supplies the limit for you.

## Options

| Flag | Default | Meaning |
|------|---------|---------|
| `-o`, `--out` | `<stem>.svg` | output artwork SVG for a single mesh |
| `--out-dir` | — | output folder; required for several inputs |
| `--report` | — | JSON report (one record per mesh) |
| `--jobs` | `1` | meshes rendered in parallel |
| `--skip-existing`, `--fail-fast`, `-q` | off | batch controls, as in `img2plot` |
| `--slices` | `8` | cross-sections along the thinnest axis |
| `--fit-mm` | `180` | largest side-view extent in millimetres |
| `--margin-mm` | `5` | clearance around the artwork |
| `--stroke-mm` | `0.2` | plotter pen width |

Exit codes: `0` all meshes rendered, `1` at least one failed, `2` bad usage
or option value. Progress goes to stderr, so stdout stays script-friendly.

## Tests and preview

```bash
python3 -m pytest backend/tests/test_penplot_stl_views.py -q
rsvg-convert -b white -w 1400 backend/tests/fixtures/knipexXs.svg \
  -o knipex-side-preview.png
```

The pure tests skip cleanly in `backend/.venv` (where the external tools are
absent) and run fully with `python3`. Live-server tests for
`/v1/compose-sheet` live in `test_penplot_compose_sheet.py`.

See `docs/stl-svg-generation-toolchain.md` for the full tool/capability
inventory and `mem:tooling/penplot-svg-toolchain` in Serena memory.
