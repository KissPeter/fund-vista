- Status: in progress
- Created: 2026-09-13
- Last updated: 2026-09-13

# B-001 Page artwork touches frame — missing internal padding

Bug: `layout()` in `backend/penplot/optimize.py:224` scales artwork to fill
`margin-to-margin` exactly. When image aspect matches the draw rect, outer
strokes land on `margin_mm` — same line as `page_frame_rect()` and the label
divider. No breathing room; `linemerge` (default 0.5mm) can even fuse them.
Only the label case has a gap (`LABEL_ARTWORK_GAP_MM=1.0`).

Fix (agreed): new `PageParams.padding_mm` option, default `5.0`, surfaced in
`Page & pen` UI section. Artwork area = margin + padding on all sides
(plus existing `reserve_bottom_mm` for label).

## Phases

- [x] Repro: matching-aspect image touches `margin_mm` rect
- [x] `PageParams.padding_mm: float = 5.0 (0..20)` in `schemas.py`
- [x] `layout()` + `layout_scale()` accept `padding_mm`, shrink `draw_w/draw_h`, offset `ox/oy`
- [x] `pipeline.py` passes `params.page.padding_mm` in both calls
- [x] UI slider in `_page_pen.html` + JS in `_convert.html` if needed
- [x] Tests: layout inset, default 5, 0 = legacy, convert does not touch frame

## Definition of Done

- [ ] Code rules: settings-only env, Pydantic v2, route registration untouched
- [ ] TDD red → green → refactor
- [ ] Full suite green (`pytest backend/tests`)
- [ ] Docs + Serena memory synced
- [ ] Commit per phase, push only when green
