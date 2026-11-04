# Title placement: inside or outside the page border

Status: implemented (phase 1, backend). `inside` stays the default and its output is byte-identical; outside placements live in `labels.py`, `pipeline.py`, `compose_sheet.py`, `layout_svg.py` and `stats_table.py`.

### Implementation decisions (phase 1)

- Subtitle cap height is `labels.SUBTITLE_HEIGHT_RATIO = 0.6` of the title height, one constant.
- Title and subtitle are stacked with `labels.OUTSIDE_LINE_GAP_MM = 1.0` between them.
- Shared limits are kept: 41 characters per line, two lines (`lines`), 52 characters for title plus subtitle in the shop.
- Default placement is `inside`. Its cache key is unchanged because `placement` is dropped from the key when it is `inside`.
- `label.lines` is optional on convert and replaces `label.text` when it carries text. Inside, it is drawn as `title - subtitle`.
- Compose keeps `lines` top-level and does not accept them inside `layout.label`.
- Outside placements carry the sheet bounds as `data-kp-a4-bounds="0 0 W H"` on the root `<svg>`, not as an `a4-bounds` rect, so inside output stays byte-identical. Fallback applied for N7: vpype 1.15.0 plots a fill-none, stroke-none rect as a path (sample sheet: 40 paths / 6674.6 mm with the rect, 38 paths / 3964.7 mm without), so the rect is not emitted.
- N7 verified with real vpype 1.15.0 (`backend/tests/test_penplot_vpype_bounds.py`, skipped without vpype): the emitted sheet plots the same as the same sheet without the bounds attribute, and the per-ink sheets share one viewBox. vpype 1.15.0 runs on Python 3.12 only in a separate scratch venv, because `pip install vpype` into the Python 3.14 fund-vista venv fails (vpype 1.9.0 pins Shapely 1.8.0, which has no wheel for 3.14).
- Measured deviation: N6 (2 KB for the rect plus the second line) does not hold. A glyph line costs about 6.5 KB in the default face at any size, so the test guards the bounds attribute (under 200 bytes) and that a subtitle costs at most one title line.
- Not run here: the live-server (HTTP) suites, because the venv lacks `httpx_retries`; the 422 checks are covered at the schema level.

## Goal

Let the user place the title either **inside** the page border (current behaviour, title strip at the bottom of the frame) or **outside** it, **below** or **above** the page. Reference: a competitor shows the title as a caption under the map frame. The choice sits in the Page & pen rail as three icons, like orientation.

## Decisions

- **Final sheet stays A4.** Outside titles never grow the sheet. A plotter can only draw on paper, so the page frame gives up space instead.
- **Frame shrinks on the title's side.** Below: the frame's bottom edge moves up by one title strip. Above: its top edge moves down.
- **Artwork and tables keep clear.** The artwork and the stats/context tables reserve the strip plus the usual 1 mm artwork gap, on the title's side.
- **No title band outside the border.** The band exists to join the page frame; outside, there is no frame to join. Lettering only.
- **Gallery compose follows the same rule.** The gallery sheet is also A4 and gets the same placement.
- **Defaults keep old output.** `inside` is the default and is left out of the cache key, so existing cache entries stay valid.

## Geometry

Let `strip = pad + text_h + pad`, with `pad = max(0.3 * height, 1 mm)`, as in `labels._strip_metrics`.

| Placement | Frame | Artwork and tables reserve | Lettering anchored to |
|---|---|---|---|
| inside (default) | margin rect, band inside it | `text + pad + gap` (today's behaviour) | band bottom |
| below | bottom edge at `page_h - margin - strip` | `strip + 1 mm` on the bottom | bbox bottom at `page_h - margin` |
| above | top edge at `margin + strip` | `strip + 1 mm` on the top | bbox top at `margin` |

## Code touch points

- `backend/penplot/labels.py`: `label_outside_mm()`, `page_frame_rect(top_mm, bottom_mm)`, `render_label(placement=...)` with no band outside.
- `backend/penplot/stages/layout_svg.py`: `layout()` and `layout_scale()` take `reserve_top_mm`.
- `backend/penplot/stats_table.py`: `layout_stats_table()` takes `reserve_top_mm`; top-corner tables clear an above-title.
- `backend/penplot/schemas.py`: `LabelPlacement = Literal["inside", "below", "above"]`, field `LabelParams.placement`.
- `backend/penplot/pipeline.py`: choose reserves and frame insets per placement; pass them through `_generate_from_mask`, `layout`, the tables and the frame; drop `placement` from the cache key when it is `inside`.
- `backend/penplot/compose_sheet.py`: the same placement for the gallery sheet (stack at the bottom, or shifted to the top for `above`; frame and artwork area follow).
- `backend/penplot/templates/partials/_page_pen.html`: three radio icons, `name="label_placement"`, after orientation.
- `backend/penplot/templates/partials/_convert.html`: read the selected placement into `label.placement`.
- `backend/penplot/templates/partials/_head.html`: icon-toggle styles.

## Out of scope here

- **Gallery UI controls.** The gallery page lives in the shop repository (`galleryCompose.ts`), not in this one. The API field is added here; the matching control must be added in the shop.
- **Paper size.** Some plotters need margin for the title. Confirm the physical sheet before enabling outside titles for plotting.

## Verification plan

- Unit tests: lettering sits on the margin in its strip; no band outside; frame inset equals the strip; artwork stays clear of the strip; `layout_scale` matches `layout` with a top reserve.
- Existing penplot and airport suites must stay green, including the cache-key tests.
- Visual check: render inside, below and above for one A4 sample; confirm the sheet stays A4 and nothing overlaps.
