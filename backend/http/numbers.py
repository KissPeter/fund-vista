"""Shared numeric parsing helpers (REF-002 Phase 1, fix-once home).

``fnum`` lived as two byte-identical copies in ``airports/router.py``
and ``airports/render.py`` (OurAirports CSV rows carry empty strings
for missing numbers) — one definition now serves both.
"""

from __future__ import annotations


def fnum(value: object) -> float | None:
    """Parse ``value`` to float; None/blank/unparseable → None."""
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


__all__ = ["fnum"]
