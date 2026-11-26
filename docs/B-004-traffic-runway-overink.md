- Status: in progress
- Created: 2026-11-26
- Last updated: 2026-11-26
- Owner: Péter Kiss (peter.kiss@pepperstone.com)

# B-004 — Airport traffic SVG re-inks the runway funnel

Around the runways every arrival/departure trace runs on practically the
same geometry (final approach to the threshold, the climb-out leg). The
traffic renderer emitted one `<path>` per movement, so hundreds of strokes
landed on the same few millimetres of paper — each one a full pen pass that
over-inks the sheet and tears it. `linemerge` in the plot pipeline cannot fix
this: it joins strokes that touch at their *endpoints*, not strokes drawn on
top of each other.

This was optimized once (seed-only corridor collapse), but a synthetic
200-flight funnel still produced 23 full passes. Raising the geometric
tolerance to reach 10 passes over-merged genuinely distinct parallel
corridors, so the accepted direction (owner decision 2026-11-26) is
union-based collapse plus a request-level density knob, capped at ~10 passes
in a busy funnel. Backend only: no shop UI change.

## Scope

- `backend/traffic/corridors.py`: a stroke joins the closest corridor whose
  rolling union of recent accepted geometries covers ≥85 % of its samples;
  a diverging tail is emitted only as a short extra pass. One corridor
  contributes at most `max_corridor_passes` full passes (10 by default, one
  per ~20 movements).
- `backend/traffic/schemas.py`, `router.py`, `render.py`: new bounded
  `max_corridor_passes` field threaded into the one `render_svg` path, so
  preview and plot stay identical. Movement counts stay *movement* counts.
- `backend/traffic/render_cache.py`: the knob joins `_filter_fingerprint`
  and `RENDER_VERSION` moves `r7 → r8`, expiring cached r7 SVGs.

## Definition of Done

- [ ] Code rules: the new field is a Pydantic v2 `Field` with bounds, no new
      environment setting, and route registration is unchanged.
- [ ] TDD red → green: unit tests cover identical and jittered retraces,
      distinct/crossing corridors, a drifting funnel, diverging tails,
      sub-pen stubs, the 10-pass cap and the render-time budget.
- [ ] Real-HTTP validation covers the new knob's valid and invalid values
      (0, 51, extra field forbidden); no `TestClient` for API behavior.
- [ ] One render path: no shop UI change; preview and plot both go through
      `render_svg`, and the cache key distinguishes pass densities.
- [ ] Full backend suite green.
- [ ] `docs/index.md` and Serena memory `backend/traffic/corridors` synced.
- [ ] Implementation committed on a dedicated worktree branch, pushed only
      when green, and a PR opened and linked to the session.

## Notes

- Calibration: synthetic 200-flight identical funnel — old seed-only
  tol=2/split=8 → 23 passes; union tracking with cap=10 → exactly 10 at any
  traffic volume. Tolerance alone (tol=4/split=20) also reached 10 but
  over-merged distinct corridors.
- Worktree: `~/PycharmProjects/fund-vista.worktrees/27`, branch
  `work/B-004-traffic-runway-overink`, based on clean `origin/main`
  (`4e82412`). The main checkout keeps its unrelated dirty files untouched.
