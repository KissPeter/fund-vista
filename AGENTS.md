## Subagent cost (mandatory)

The key words MUST, MUST NOT, SHOULD and MAY are to be interpreted as
described in RFC2119.

- A subagent SHOULD run on the cheapest model that can do the task (for
  example Haiku) rather than a more expensive one (Sonnet, Opus).
- A subagent MAY run on a more expensive model only when the task needs it
  (design-heavy work, subtle debugging), and the agent MUST say so in one line.
- The agent MUST NOT use a more expensive model for a subagent merely out of
  habit; mechanical edits, searches, test runs and PR chores SHOULD go to the
  cheap model.
- Instructions written into this repository MUST use RFC2119 key words.

## One sheet source (mandatory)

The key words MUST, MUST NOT and SHOULD are to be interpreted as described in RFC2119.

- `POST /v1/compose-sheet` MUST remain the only composer of every gallery sheet the pen-pixel shop previews or delivers (preview, personalisation, order file). It MUST keep one code path for them.
- A request with `artwork: "inline"` MUST answer a plot-ready sheet with no `<image>` and no `<text>`. The `artwork` field MUST be accepted by the `extra="forbid"` models and MUST default to `"image"`.
- A change to sheet geometry MUST keep the preview and the inline output on the same placement.
- A deferred follow-up MUST get a ticket with an owner in the same change, never only a commit message.
