"""GET a URL and print the response body (tiny, stdlib only).

Replaces the inline ``python -c "import urllib.request; ..."`` probes the
deploy script carried inside ``docker run`` command strings (issue #64).

Usage::

    python -m backend.tools.http_get http://fundvista:8100/healthz

Exit code 0 means the request succeeded (the body is printed), 1 means it
failed (network error, non-2xx status, timeout). The deploy script only
needs the two facts, so the body goes to stdout and the error detail to
stderr.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request


def fetch(url: str, timeout: float = 8.0) -> str:
    """Return the response body of ``url``; raise on any transport/HTTP error."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8", "replace")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: exactly one URL argument."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m backend.tools.http_get <url>", file=sys.stderr)
        return 2
    try:
        body = fetch(args[0])
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"http_get failed: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
