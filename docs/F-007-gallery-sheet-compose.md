# F-007 — Gallery sheet compose on the backend

- Status: in progress
- Created: 2026-10-30
- Last updated: 2026-10-30

The key words MUST, MUST NOT, SHOULD and MAY are to be interpreted as
described in RFC 2119.

## Phase 1 (this change): `POST /v1/compose-sheet`

- The endpoint MUST be stateless and public, use the same per-IP rate
  limiter as the other `/v1` routes, and MUST NOT take an `image_id`.
- The request is JSON: `svg`, `ink_hex`, `layout` (`page`, `label`),
  `lines` (title then subtitle) and `context_table`. Field names, ranges and
  defaults MUST equal `PageParams`, `LabelParams` and `ContextTableParams`
  (`backend/penplot/schemas.py`); the compose models reuse those definitions
  and a test pins them.
- The artwork MUST be embedded unchanged as one `<image>` (base64 data URI)
  and MUST NOT be re-parsed or re-simplified. It MUST be fitted into the
  free area between the margin and the title block, keeping its own aspect.
- The frame, title-block band, lettering and context table MUST be drawn with
  the existing `labels.page_frame_rect`, `labels._band_rect`,
  `labels.render_label` and `stats_table.layout_stats_table`.
- The sheet is A4 (210 x 297 portrait, 297 x 210 landscape); the response is
  `image/svg+xml`, byte-identical for the same request.
- Markers MUST be kept: `data-kp-frame`, `data-kp-title-block`,
  `data-kp-label`, `data-kp-text`, `data-kp-context`,
  `data-kp-context-border`.
- Validation MUST match `POST /v1/images` for the SVG (size cap, sniff,
  `parse_svg_vectors`). Invalid input MUST return 422 with the error
  envelope; an oversize body MUST return 413; no valid input may return 5xx.
- Logs MUST carry sizes and counts only, never the title, the table text or
  the artwork.

### Known limits (Phase 1)

- The context table is drawn over the artwork; an `<image>` cannot be
  knocked out the way strokes are, so it sits on top of the art.
- Context cell lettering carries no per-cell `data-kp-text` marker.
- Band placement follows the shop mock's stacked-row model, not the
  single-line band of `render_label(border=True)`, which differs by a
  fraction of a millimetre.

## Phase 2 and 3 (not in this change)

- Phase 2: shop switch behind `VITE_GALLERY_BACKEND_COMPOSE=1`, calling the
  same-origin `/v1/compose-sheet`.
- Phase 3: the order artifact moves to the backend output.

## Definition of Done (Phase 1)

- [x] Endpoint, request model and router registration
- [x] Tests: band and markers, pads, right pad 0 vs 5, frame on/off,
      orientation, context table on/off, determinism, 422 and 413 cases,
      schema parity with the convert models, 5 MB under 2 s
- [ ] CI gate green (`backend-tests` workflow)
- [ ] Merged to `main`
