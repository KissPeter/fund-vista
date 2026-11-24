# STL three-view custom-design plan — backend (2026-11-24)

- Source: planning subagent (session `ses_df178d83bffejc6qVYAaWJ4fWW`), repo inspection of
  `backend/penplot/` and the shop worktree. Companion plan:
  `pen-pixel-shop/docs/stl-custom-design-plan.md`.
- Goal: a customer uploads an STL; the service returns three separately selectable
  artwork-only views; the shop combines selected views into one plot-ready sheet or
  adds each view as its own plot item.

## Invariants (MUST / MUST NOT)

- `POST /v1/compose-sheet` MUST remain the only composer of gallery/order sheets.
  The STL endpoints MUST NOT compose sheets.
- Artwork for `artwork: "inline"` MUST be paths only: no `<text>`, no `<image>`,
  no sheet furniture. `compose_sheet_svg(body, *, max_svg_bytes)` MUST stay keyword-only.
- `ComposeSheetRequest.artwork` MUST stay accepted by `extra="forbid"`, MUST default
  to `"image"`, and `"inline"` MUST keep one placement path for preview and order.
- `slice3d` / `trimesh` MUST NOT be added to `backend/uv.lock` (shop licence-inventory
  digest is gated on it — `docs/CR-008-shop-licence-inventory-coupling.md`).
- Raw view SVGs MUST NOT become order files; only stored `_stlsheet.svg` output from
  the composer may be fulfilled.
- Retention MUST follow `docs/nfr-retention.md`: 24 h unordered, 90 d ordered.

## Owner decisions before coding

1. Broad face is `[long, mid]` (slice along thinnest axis) — the view the owner
   confirmed on 2026-11-24. The earlier rejection of end/edge views for the gallery
   is superseded by the explicit request for three views in custom design.
2. Combined sheet = one cart line priced from summed server-side stats;
   separate views = one line each. Confirm before pricing work.
3. Rights acceptance checkbox in the UI is mandatory before cart add.
4. Sample STL rights (Knipex XS, crescent pair) remain open before sale.
5. Production install owner for the external Python toolchain MUST be named in a
   ticket in the same change if deployment is deferred.

## View table

| Key | Slice axis | Projected axes | Meaning |
|---|---|---|---|
| `broad` | thinnest | `[long, mid]` | recognisable face, length horizontal |
| `edge` | second | `[long, thin]` | thickness/profile |
| `end` | longest | `[mid, thin]` | end-on |

`canonical_axes(mesh) -> (thin, mid, long)`. Axes MUST be ranked by `(extent, axis_index)`
so equal extents stay stable. `render_side_view` stays as a compatibility alias of
`broad`. Determinism: fixed slices/fit/margin/stroke, 0.06 mm simplification,
duplicate-point removal, direction normalisation and polyline sorting. Output goes
through `backend.penplot.stages.layout_svg.to_svg`.

## API

### `POST /v1/stl/import`
Multipart `file` (ASCII/binary `.stl`), same rate-limit and size-cap pattern as
`/v1/images`. Returns `image_id` (SHA-256 of bytes), `format`, `filename`, `faces`,
`extents_mm`, `canonical_axes`, `warnings`, `expires_at`. Errors: 413 payload too
large, 415 unsupported type, 422 `stl_invalid` / `stl_too_heavy`, 429 rate limited.

### `POST /v1/stl/views`
`extra="forbid"` body: `image_id`, `params` (`views`, `slices`, `fit_mm`,
`margin_mm`, `stroke_mm`). Returns `image_id`, `generator_version`, `views[]` with
`view`, `svg_url`, `meta_url`, `stats` (strokes, pen-down mm, pen-up mm, estimated
seconds). Stats MUST be computed server-side. Cache by
`(image_id, view, generator_version, params_hash)`. Store `results/<id>_<view>_stlview.svg`
plus `.json` sidecar. Missing libraries MUST return a 503 `stl_toolchain_unavailable`
envelope, never a raw `ModuleNotFoundError`.

### `POST /v1/compose-sheet` (extension)
Add `artworks: list[ComposeSheetArtwork]` (max 3, each `svg`, strict `result_id`,
optional `view`) and `store: bool`. Exactly one of `svg` / non-empty `artworks`.
One artwork preserves current placement byte-for-byte; two/three use deterministic
layouts in the same art area. One `_plan_sheet` result drives PNG preview and inline
output. `store: true` requires inline + SVG preview and writes
`results/<sha>_stlsheet.svg` with a sidecar carrying aggregated `ConvertStats`,
source image id, ordered views, generator version and params hash; response headers
`X-Result-Url` / `X-Result-Sha256`.

Result/meta filename gates MUST accept `_stlview.svg` and `_stlsheet.svg` (basename
only). `/v1/images/{image_id}/retain` MUST promote derived STL files and sidecars.

## Config caps

`stl_max_faces` 250000, `slices` 2–16, `fit_mm` 20–400, `margin_mm` <= 20,
`stroke_mm` 0.1–1.0, all with `PENPLOT_*` env overrides. Reuse `max_upload_bytes`.

## Tasks (dependency order)

| # | Task | Depends on |
|---:|---|---|
| T1 | Generalise `stl_views.py`, projection table, deterministic tests | decisions |
| T2 | Config caps and toolchain-unavailable errors | T1 |
| T3 | `/v1/stl/import`, storage, result/meta gate changes | T2 |
| T4 | `/v1/stl/views`, caching, stats sidecars, retain promotion | T3 |
| T5 | Extend `/v1/compose-sheet` (multi-panel + `store`) | T1, T3 |
| T6 | Backend HTTP tests and full backend gates | T4, T5 |
| T7 | Runtime install runbook + ticket, no `uv.lock` change | T2 |

## Tests / gates

`backend/tests/test_penplot_stl_views.py` gains projection-table, determinism and
artwork-only cases; new `test_penplot_stl_api.py` covers import, cache, caps,
`extra="forbid"`, 503 envelope, result/meta gates, retain promotion, multi-view
preview/inline parity and stored sidecars; `test_penplot_compose_sheet.py` gains
legacy-path parity tests.

```bash
backend/.venv/bin/python -m pytest backend/tests/ -q
python3 -m pytest backend/tests/test_penplot_stl_views.py -q
ruff check backend/penplot backend/tests
```

## Main risks

CPU abuse by large meshes (caps + shared rate limit), TTL expiry of ordered derived
files, placement drift between preview and inline, and toolchain absence in production.

## T7 runtime-install ticket (open)

Production deployment is deferred; the named install owner is Péter Kiss
(peter.kiss@pepperstone.com). Follow-up ticket:
`docs/B-003-stl-toolchain-runtime-install.md` (status `todo`). `backend/uv.lock`
MUST stay unchanged, and sample STL rights (Knipex XS, crescent pair) MUST stay
blocked before sale while shop ticket S15 is open.
