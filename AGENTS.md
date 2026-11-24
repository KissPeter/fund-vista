# AGENTS.md - Tier 1 (Canonical, Always-on)

## Principles

- **MUST** prioritize correctness over speed.
- **MUST NOT** mark work complete without verification.
- **MUST** think before coding: state assumptions, surface ambiguity, and ask when unclear.
- **SHOULD** prefer the simplest solution that fully satisfies the request.
- **MUST** keep changes surgical; every changed line **MUST** trace to the task.
- **MUST** define explicit, verifiable success criteria before implementation.
- **MUST** run validation proportionate to risk; if uncertain, **MUST** run the full relevant checks.
- **MUST** stop and provide exact remediation if blocked (cannot run tests/start services/fix failures).
- **MUST** use Serena code-intelligence tools instead of manual file operations for all code discovery and edits.
- **SHOULD** use shell mainly for git, tests, lint, dependency checks, service startup and operations with no Serena equivalent.
- **MUST** follow user preferences unless they conflict with higher-priority instructions.
- Instructions written into this repository **MUST** use RFC2119 key words.

## Serena-first tooling (mandatory for every agent and subagent)

The key words MUST, MUST NOT, SHOULD and MAY are to be interpreted as described in RFC2119.

- Every subagent **MUST** use Serena as the first and primary tool for code discovery and code edits.
- For symbol navigation use `get_symbols_overview`, `find_symbol`, `find_referencing_symbols`, `find_declaration`, `find_implementations` and `get_diagnostics_for_file` where exposed by the active toolset.
- For pattern search use `search_for_pattern` (never `grep`, `rg`, `glob`, `shell cat` or `find`) for source code, tests and scripts.
- For edits use `replace_content`, `replace_in_files`, `replace_symbol_body`, `insert_before_symbol`, `insert_after_symbol`, `rename_symbol` and `safe_delete_symbol` (never the harness `edit` or `write` tools) whenever an equivalent Serena tool exists.
- A subagent **MUST NOT** use the harness `read` tool to re-read a source file after a Serena edit succeeded; Serena edits are atomic and trusted.
- The orchestrator **MUST** include "Read AGENTS.md before acting; Serena-first is mandatory" in every subagent prompt launched in this repository.
- Before any Serena edit inside a git worktree, the subagent **MUST** activate the Serena project at that worktree path, or use absolute paths, and **MUST** run `git status` in the main checkout afterwards. A Serena success message at the wrong project is not proof.
- If the active Serena toolset genuinely lacks a primitive (for example brand-new file creation), the subagent **MUST** use the harness file operation only for that bootstrap, **MUST** immediately stage the new file with `git add`, and **MUST** state the missing primitive in its final answer.
- Durable project learnings **MUST** be persisted with Serena memory tools (`write_memory`, `read_memory`, `edit_memory`), not ad-hoc markdown outside `docs/`.

## Subagent cost (mandatory)

The key words MUST, MUST NOT, SHOULD and MAY are to be interpreted as described in RFC2119.

- A subagent SHOULD run on the cheapest model that can do the task (for
  example Haiku) rather than a more expensive one (Sonnet, Opus).
- A subagent MAY run on a more expensive model only when the task needs it
  (design-heavy work, subtle debugging), and the agent MUST say so in one line.
- The agent MUST NOT use a more expensive model for a subagent merely out of
  habit; mechanical edits, searches, test runs and PR chores SHOULD go to the
  cheap model.
- Instructions written into this repository MUST use RFC2119 key words.

## Product context

**MUST** load the relevant Serena memory (`project_overview`, `impl/roadmap`) before architecture or data-model decisions.

**MUST** load the docs named below when their trigger applies:

| Doc | When to load |
|---|---|
| `docs/index.md` | Any feature implementation, scoping or behavior-ambiguity resolution |
| `docs/stl-custom-design-plan.md` | Any STL three-view / custom-design backend work |
| `docs/CR-008-shop-licence-inventory-coupling.md` | Before changing `backend/uv.lock` or any dependency manifest |
| `docs/api-integration-testing.md` | Writing or running HTTP API tests |
| `docs/convert-pipeline-reference.md` | Convert / compose pipeline work |
| `docs/svg-metadata.md` | Provenance, ownership or licence metadata |
| `docs/mac-cd.md`, `docs/mac-fundvista.md` | Deployment to the Mac host |

## Project structure

| Path | Role |
|---|---|
| `backend/` | FastAPI / Python service |
| `backend/penplot/` | Sheet composer, render stages, STL views, schemas and router |
| `backend/tests/` | pytest suite (real HTTP for endpoint behavior) |
| `docs/` | Feature records, change requests, bugs, plans and contracts |
| `.opencode/skills/` | OpenCode skills (RFC2119 normative language) |
| `.claude/skills/` | Mirrored OpenSpec skills |
| `.serena/` | Serena project config and memories |

## Skills index

**MUST** load and follow the relevant skill whenever its trigger condition is met.

| Skill | Path | When to invoke |
|---|---|---|
| `test-design` | `.opencode/skills/test-design/SKILL.md` | Adding or changing any backend endpoint, job or pure algorithm |
| `document-findings` | `.opencode/skills/document-findings/SKILL.md` | Documenting research findings into `docs/` and Serena memory |
| `deploy-mac` | `.opencode/skills/deploy-mac/SKILL.md` | Deploying the backend to the Mac host |
| `track-work` | `.opencode/skills/track-work/SKILL.md` | Creating or updating F/CR/B work tracking docs |
| `prepare-plotter-svg` | `.opencode/skills/prepare-plotter-svg/SKILL.md` | Converting images to plotter SVGs or QA for the gallery |
| `openspec-apply-change` | `.opencode/skills/openspec-apply-change/SKILL.md` | Implementing an OpenSpec change |
| `openspec-propose` | `.opencode/skills/openspec-propose/SKILL.md` | Proposing an OpenSpec change |
| `openspec-explore` | `.opencode/skills/openspec-explore/SKILL.md` | Exploring an idea before or during a change |
| `openspec-update-change` | `.opencode/skills/openspec-update-change/SKILL.md` | Revising an existing change's planning artifacts |
| `openspec-sync-specs` | `.opencode/skills/openspec-sync-specs/SKILL.md` | Syncing delta specs to main specs |
| `openspec-archive-change` | `.opencode/skills/openspec-archive-change/SKILL.md` | Archiving a completed change |

## Non-functional requirements (mandatory)

- **MUST NOT** add `slice3d` or `trimesh` to `backend/uv.lock` without a shop-side licence-inventory regeneration in the same change.
- API behavior tests **MUST** use real HTTP (in-process uvicorn or live subprocess + httpx). They **MUST NOT** use `fastapi.testclient.TestClient`, even for validation-only paths.
- CPU-heavy endpoints **MUST** enforce face, size, rate and parameter caps; negative boundary tests are part of the change.
- Derived STL files **MUST** follow retention: 24 h unordered, 90 d ordered (`pen-pixel-shop/docs/nfr-retention.md`).
- User-uploaded STLs and sample assets **MUST** keep provenance and licence caution; sample sale stays blocked while S15 is open.

## One sheet source (mandatory)

The key words MUST, MUST NOT and SHOULD are to be interpreted as described in RFC2119.

- `POST /v1/compose-sheet` MUST remain the only composer of every gallery sheet the pen-pixel shop previews or delivers (preview, personalisation, order file). It MUST keep one code path for them.
- A request with `artwork: "inline"` MUST answer a plot-ready sheet with no `<image>` and no `<text>`. The `artwork` field MUST be accepted by the `extra="forbid"` models and MUST default to `"image"`.
- A change to sheet geometry MUST keep the preview and the inline output on the same placement.
- A deferred follow-up MUST get a ticket with an owner in the same change, never only a commit message.

## Governance note

- **MUST NOT** add new mandatory rules outside this file without updating the relevant index in the same change.
- Skills **MUST** live under `.opencode/skills/*/SKILL.md` and **MUST** use RFC2119 normative language.
- Reviews and fixes of a change **MUST** use Serena for all source inspection; a review that only used shell search is not valid.
