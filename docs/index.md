# Work index

Naming: `F-<n>` feature, `CR-<n>` change request, `B-<n>` bug.
Status: `todo` | `in progress` | `done`.
This file MUST be updated (status + last updated) on every change to a
tracked item — see `.opencode/skills/track-work/SKILL.md`.

| ID | Type | Title | Status | Last updated |
|----|------|-------|--------|--------------|
| CR-001 | change request | Split citymap out of `/penplot` into `/citymap` | done | 2026-09-13 |
| F-001 | feature | Airport ground-diagram blueprint generator (`/airports`) | done | 2026-09-13 |
| F-002 | feature | Dropped aeroway elements (stands/stopways) + surrounding context | done | 2026-09-14 |
| F-003 | feature | Freeform + IATA airport search | done | 2026-09-14 |

Doc: `docs/CR-001-citymap-page-split.md`.
Doc: `docs/F-001-airport-diagrams.md`.

## Research notes (untracked, no ID)

- `docs/drawscape-map-source-findings.md` — Drawscape `/api/map-render`
  uses Mapbox Streets v8 (paid).
- `docs/hungary-europe-map-providers.md` — free HU/EU provider options.
- `docs/drawscape-two-phase-rendering.md` — two-phase preview → render
  vs fund-vista static SVG.
