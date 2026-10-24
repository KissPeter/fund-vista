"""Print one field of a JSON document read from stdin.

Replaces the inline ``python3 -c 'import json,sys; ...'`` snippets that the
ops scripts used to carry (issue #64: "never prepare inline python script —
you can invoke a tiny python function").

Usage::

    curl -s "$API/v1/jobs/$id" | python3 -m backend.tools.json_field status
    curl -s "$API/v1/jobs/$id" | python3 -m backend.tools.json_field error.code

The dotted path walks objects and list indexes (``items.0.id``). Missing
paths print nothing and exit 1, so a shell caller can branch on the exit
code (``|| echo "unknown"``).
"""

from __future__ import annotations

import json
import sys
from typing import Any


def get_field(document: Any, path: str) -> Any:
    """Return the value at the dotted ``path``; ``None`` when it is absent."""
    current = document
    for part in path.split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


def render(value: Any) -> str:
    """The print representation of a field value (objects/arrays as JSON)."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: JSON on stdin, one dotted path as the only argument."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python3 -m backend.tools.json_field <dotted.path>",
              file=sys.stderr)
        return 2
    try:
        document = json.load(sys.stdin)
    except ValueError:
        return 1  # not JSON (empty body, HTML error page, truncated stream)
    value = get_field(document, args[0])
    if value is None:
        return 1
    print(render(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
