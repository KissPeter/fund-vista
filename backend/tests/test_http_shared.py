"""Shared router scaffolding: one error envelope, one fnum, one client_ip.

REF-002 Phase 1 fix-once cover: the helpers extracted from the four v1
routers (citymap, airports, penplot, jobs) plus ``airports/render.py``
so a bug in any of them is fixed exactly once.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from backend.http import client_ip, error_response, fnum


def test_error_response_shape():
    resp = error_response(404, "city_not_found", "No place found for 'X'.")
    assert resp.status_code == 404
    assert json.loads(bytes(resp.body).decode()) == {
        "error": {"code": "city_not_found", "message": "No place found for 'X'."}
    }


def test_error_response_forwards_headers():
    resp = error_response(
        429, "rate_limited", "Too many requests.",
        headers={"Retry-After": "60"},
    )
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "60"


def _request(headers=None, host="9.9.9.9"):
    return SimpleNamespace(
        headers=headers or {},
        client=SimpleNamespace(host=host) if host is not None else None,
    )


def test_client_ip_prefers_forwarded_for():
    req = _request(headers={"x-forwarded-for": "1.2.3.4, 5.6.7.8"})
    assert client_ip(req, trust_forwarded_for=True) == "1.2.3.4"


def test_client_ip_ignores_forwarded_for_when_untrusted():
    req = _request(headers={"x-forwarded-for": "1.2.3.4"})
    assert client_ip(req, trust_forwarded_for=False) == "9.9.9.9"


def test_client_ip_falls_back_to_unknown():
    assert client_ip(_request(host=None)) == "unknown"


def test_fnum_parses_numbers():
    assert fnum("3.5") == 3.5
    assert fnum(5) == 5.0
    assert fnum(" 12 ") == 12.0


def test_fnum_maps_missing_to_none():
    assert fnum(None) is None
    assert fnum("") is None
    assert fnum("   ") is None
    assert fnum("abc") is None
    assert fnum(object()) is None
