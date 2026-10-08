"""Shared client-IP resolution (REF-002 Phase 1, fix-once home).

The penplot rate limiter keys off the client IP. ``penplot/router.py``
and ``jobs/router.py`` each carried their own copy of this helper —
one definition now serves both. The ``trust_forwarded_for`` flag comes
from the penplot settings (only correct behind a proxy that overwrites
``X-Forwarded-For``); callers pass it explicitly so this module stays
settings-free. Trust model (penplot review D.1.2): the first XFF hop is
trusted unconditionally only behind a proxy that OVERWRITES the header;
with trust off, the limiter keys off the socket peer so a directly
exposed instance can't be walked around by rotating XFF values.
"""

from __future__ import annotations

from fastapi import Request


def client_ip(request: Request, *, trust_forwarded_for: bool = True) -> str:
    """Best-effort client IP; "unknown" when unknowable. Never raises."""
    # Broad catches are deliberate: IP resolution must never fail a request
    # (a broken header/client object degrades to "unknown", same rate-limit
    # bucket as before — matching both original helpers' behavior).
    try:
        if trust_forwarded_for:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                head = forwarded.split(",")[0].strip()
                if head:
                    return head
    except Exception:
        pass
    try:
        if request.client is not None:
            return request.client.host
    except Exception:
        pass
    return "unknown"


__all__ = ["client_ip"]
