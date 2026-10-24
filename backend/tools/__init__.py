"""Tiny, importable CLI helpers for the ops scripts.

The ops shell scripts used to embed Python one-liners (``python3 -c '...'``)
and heredocs. Those are unreadable, untestable and impossible to reuse — the
same problem the affilio repo hit. The helpers here are ordinary Python
functions with a ``main()`` entry point, so the shell scripts call a module
instead of carrying a script inside a string:

    curl -s "$API/v1/jobs/$id" | python3 -m backend.tools.json_field status

Stdlib only: the helpers must run inside the production image and on a
plain host checkout without extra dependencies.
"""

from __future__ import annotations
