"""Weather map backend. Phase 1: availability, field fetch, contours, streamlines,
H/L extrema. Phase 2: layered artwork (render), the render cache, and import.

No sheet composition lives here: sheets are composed only by
``POST /v1/compose-sheet`` (see AGENTS.md "One sheet source").
"""
