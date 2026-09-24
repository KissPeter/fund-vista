# Work index

Naming: `F-<n>` feature, `CR-<n>` change request, `B-<n>` bug.
Status: `todo` | `in progress` | `done`.
This file MUST be updated (status + last updated) on every change to a
tracked item — see `.opencode/skills/track-work/SKILL.md`.

| ID | Type | Title | Status | Last updated |
|----|------|-------|--------|--------------|
| CR-001 | change request | Split citymap out of `/penplot` into `/citymap` | done | 2026-09-13 |
| CR-002 | change request | Convert pipeline performance (P1–P5: result cache, fast preview, optimize rewrites, geometry caches) | done | 2026-09-23 |
| F-001 | feature | Airport ground-diagram blueprint generator (`/airports`) | done | 2026-09-13 |
| F-002 | feature | Dropped aeroway elements (stands/stopways) + surrounding context | done | 2026-09-14 |
| F-003 | feature | Freeform + IATA airport search | done | 2026-09-14 |
| F-004 | feature | Airport layer toggles (tick any) | in progress | 2026-09-14 |

Doc: `docs/CR-001-citymap-page-split.md`.
Doc: `docs/CR-002-convert-performance.md`.
Doc: `docs/F-001-airport-diagrams.md`.

## Research notes (untracked, no ID)

- `docs/drawscape-map-source-findings.md` — Drawscape `/api/map-render`
  uses Mapbox Streets v8 (paid).
- `docs/hungary-europe-map-providers.md` — free HU/EU provider options.
- `docs/drawscape-two-phase-rendering.md` — two-phase preview → render
  vs fund-vista static SVG.
- `docs/ui-penplot-image.md` — `/penplot` Jinja page: controls, JS, backend.
- `docs/convert-pipeline-reference.md` — every `/v1/convert` pipeline element:
  purpose, what it changes, its knob, cost class, and when to skip it.
- `docs/nas-fundvista.md` — NAS primary backend: topology, env, rebuild/verify/
  rollback runbook (mirrors `.opencode/skills/deploy-nas/SKILL.md`).
- `docs/ui-citymap.md` — `/citymap` Jinja page: controls, JS, backend.
- `docs/ui-airports.md` — `/airports` Jinja page: controls, JS, backend.
- `docs/python-service-shop-changes.md` — pen-pixel work-order §§1–8 implementation record.
