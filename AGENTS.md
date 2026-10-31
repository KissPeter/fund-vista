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
