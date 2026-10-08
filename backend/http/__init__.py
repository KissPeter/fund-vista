"""Shared HTTP scaffolding for v1 routers (REF-002 Phase 1).

Fix-once home for the helpers every router duplicated: the error
envelope (:mod:`backend.http.errors`), lenient number parsing
(:mod:`backend.http.numbers`), and client-IP resolution
(:mod:`backend.http.client`). Pure helpers — no router imports, so no
import cycles by construction.
"""

from __future__ import annotations

from backend.http.client import client_ip
from backend.http.errors import error_response
from backend.http.numbers import fnum

__all__ = ["client_ip", "error_response", "fnum"]
