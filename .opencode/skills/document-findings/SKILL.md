---
name: document-findings
description: Document research findings as Markdown under docs/ and persist to Serena memory, then commit md files. Use ONLY when user asks to document, record, or persist findings, investigations, or decisions.
---

# Document Findings

When invoked, do the following for every research finding in the current session:

1. Create/update a Markdown file under `docs/`:
   - Use kebab-case filenames, e.g. `docs/drawscape-map-source-findings.md`.
   - Include: date, source (curl, URL, API payload), key facts, layer/field mapping, pricing/limits if relevant, and how to reproduce free (Overpass, Geofabrik, OSMnx).
   - Never include secrets (cookies, tokens, `_shopify_*`, `cart`, `_ga`). Strip them from curl examples.
2. Persist the same summary to Serena memory:
   - Use `serena_write_memory` with a topical name, e.g. `research/drawscape-map-source`.
   - Keep it concise, link the `docs/` path.
3. Commit all md files:
   - `git add docs/*.md .opencode/skills/document-findings/SKILL.md`
   - `git commit -m "docs: <short topic>"` only (never push unless asked).
   - If commit hooks fail, fix and create a new commit, never amend a failed one.

Rules:
- Always prefer editing an existing `docs/*.md` over creating duplicates.
- Verify `docs/` and `.opencode/skills/document-findings/` exist before writing.
- Keep responses short and factual.
