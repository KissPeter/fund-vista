"""Extract a tar stream from stdin into a directory (tiny, stdlib only).

Replaces the inline ``python -c "import sys,tarfile; ..."`` in the deploy
script (issue #64). The stream mode ``r|`` is kept: the tar arrives on a
non-seekable stdin pipe, so a seekable-mode open would fail with "Illegal
seek" (seen live).

Extraction uses the ``data`` filter: absolute paths, ``..`` traversal,
device files and links pointing outside the destination are refused instead
of being written.

Usage::

    docker cp container:/data/. - | python -m backend.tools.untar_stream /to
"""

from __future__ import annotations

import sys
import tarfile


def extract_stream(destination: str) -> None:
    """Extract the tar stream on stdin into ``destination``."""
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as tar:
        tar.extractall(destination, filter="data")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: exactly one destination directory argument."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m backend.tools.untar_stream <destination>",
              file=sys.stderr)
        return 2
    try:
        extract_stream(args[0])
    except (tarfile.TarError, OSError) as exc:
        print(f"untar_stream failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
