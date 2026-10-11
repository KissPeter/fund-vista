"""Hungarian-airports-only scope (v1 product decision).

Phase 1 serves Hungarian airports only: the ingest and availability paths
reject anything else with ``unsupported_airport`` (422) instead of burning
a 3.5 GB archive pass on an airport the shop will never sell.

Two layers (both cheap):

* :func:`is_hungarian_icao` — pure ``LH`` prefix pre-check, no I/O, so
  obviously-foreign codes fail before any OurAirports/GitHub call;
* :func:`row_is_hungarian` — authoritative check on the resolved
  OurAirports row (``iso_country == HU``), covering edge cases a prefix
  cannot (closed fields, reassigned codes).
"""

from __future__ import annotations

MESSAGE = "Only Hungarian airports (LH..) are supported in v1."


def is_hungarian_icao(icao: str) -> bool:
    """True when the normalised ICAO belongs to the Hungarian ``LH`` block."""
    return (icao or "").strip().upper().startswith("LH")


def row_is_hungarian(airport_row: dict) -> bool:
    """True when the OurAirports row is a Hungarian airport."""
    return (airport_row.get("iso_country") or "").strip().upper() == "HU"


__all__ = ["MESSAGE", "is_hungarian_icao", "row_is_hungarian"]
