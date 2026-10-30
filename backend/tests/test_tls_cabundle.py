"""Readable-CA guard tests (B-001, hermetic).

On the 2026-10-20 NAS deploy a corrupted image layer made certifi's
``cacert.pem`` unreadable at the OS level (``Errno 5``) and httpx's EAGER
SSL-context build raised it inside the ``AsyncClient`` constructor —
before any network I/O — so every outbound HTTPS call 502'd while warm
Redis caches (24 h TTL) masked it except on cache-miss paths.

These tests pin ``backend/http/tls.py``: readable certifi → construction
untouched; unreadable → the first readable fallback bundle wins and TLS
stays VERIFIED (never ``verify=False``); no readable fallback → the
original OSError surfaces; the OurAirports layer wraps it into the 502
envelope with the remediation hint; ``/healthz`` surfaces the state.

The unreadable simulation uses a DIRECTORY as the fake bundle path —
``open(dir, "rb")`` raises ``IsADirectoryError`` (an ``OSError``) even as
root in the test container, unlike permission bits which root bypasses.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import ssl

import pytest

from backend.http import tls


def _fake_readable_bundle(tmp_path: pathlib.Path) -> str:
    bundle = tmp_path / "system-ca.pem"
    bundle.write_bytes(b"-----BEGIN CERTIFICATE-----\nMII\n-----END CERTIFICATE-----\n")
    return str(bundle)


def _fake_unreadable_bundle(tmp_path: pathlib.Path) -> str:
    # A directory path fails open-with-read even for root.
    bundle_dir = tmp_path / "cacert.pem"
    bundle_dir.mkdir()
    return str(bundle_dir)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)


def test_read_certifi_readable_construction_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
):
    readable_bundle = tmp_path / "certifi.pem"
    readable_bundle.write_bytes(b"-----BEGIN CERTIFICATE-----\nMII\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(tls.certifi, "where", lambda: str(readable_bundle))
    calls: list[dict] = []
    monkeypatch.setattr(tls.httpx, "AsyncClient", lambda **kw: calls.append(kw) or object())

    tls.async_client(timeout=5.0)

    # No verify override injected: httpx defaults apply verbatim.
    assert calls == [{"timeout": 5.0}]
    status = tls.ca_status()
    assert status["certifi_readable"] == "yes"
    assert "fallback_bundle" not in status


def test_unreadable_certifi_falls_back_to_readable_system_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
):
    unreadable = _fake_unreadable_bundle(tmp_path)
    fallback = _fake_readable_bundle(tmp_path)
    monkeypatch.setattr(tls.certifi, "where", lambda: unreadable)
    monkeypatch.setattr(tls, "_FALLBACK_BUNDLES", (fallback,))  # type: ignore[arg-type]
    calls: list[dict] = []
    monkeypatch.setattr(tls.httpx, "AsyncClient", lambda **kw: calls.append(kw) or object())

    tls.async_client(timeout=5.0)

    assert calls == [{"timeout": 5.0, "verify": fallback}]  # verified TLS, a path — not False
    assert tls.ca_status()["fallback_bundle"] == fallback


def test_ssl_cert_file_env_wins_when_certifi_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
):
    unreadable = _fake_unreadable_bundle(tmp_path)
    fallback = _fake_readable_bundle(tmp_path)
    monkeypatch.setattr(tls.certifi, "where", lambda: unreadable)
    monkeypatch.setenv("SSL_CERT_FILE", fallback)
    calls: list[dict] = []
    monkeypatch.setattr(tls.httpx, "AsyncClient", lambda **kw: calls.append(kw) or object())

    tls.async_client()

    assert calls == [{"verify": fallback}]


def test_caller_supplied_verify_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
):
    unreadable = _fake_unreadable_bundle(tmp_path)
    user_context = ssl.create_default_context()
    monkeypatch.setattr(tls.certifi, "where", lambda: unreadable)
    monkeypatch.setattr(tls, "_FALLBACK_BUNDLES", (_fake_readable_bundle(tmp_path),))  # type: ignore[arg-type]
    calls: list[dict] = []

    def spy(**kwargs: object):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(tls.httpx, "AsyncClient", lambda **kw: spy(**kw))
    monkeypatch.setattr(tls.httpx, "Client", lambda **kw: spy(**kw))

    # Explicit verify=False: caller keeps unverified TLS — NOT silently "fixed".
    tls.client(verify=False, headers={"x": "y"})
    assert calls[-1] == {"verify": False, "headers": {"x": "y"}}

    # Explicit SSLContext: untouched.
    tls.client(verify=user_context)
    assert calls[-1] == {"verify": user_context}


def test_no_readable_fallback_surfaces_original_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
):
    unreadable = _fake_unreadable_bundle(tmp_path)
    monkeypatch.setattr(tls.certifi, "where", lambda: unreadable)
    monkeypatch.setattr(tls, "_FALLBACK_BUNDLES", (unreadable,))  # type: ignore[arg-type]
    kwargs_seen: dict = {}

    def constructor_that_fails(**kwargs: object):  # models real httpx: builds
        kwargs_seen.update(kwargs)              # SSL context from certifi.where()
        raise OSError(5, "Input/output error", "cacert.pem")

    monkeypatch.setattr(tls.httpx, "AsyncClient", constructor_that_fails)

    with pytest.raises(OSError):
        tls.async_client(timeout=5.0)
    # No fallback found → kwargs untouched → httpx raises its original
    # OSError from the certifi read (which routers then map to 502).
    assert "verify" not in kwargs_seen


def test_healthz_reports_state(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
    from backend.main import health

    readable = _fake_readable_bundle(tmp_path)
    monkeypatch.setattr(tls.certifi, "where", lambda: readable)
    body = asyncio.run(health())
    body.pop("backend")  # build stamp (test_version.py)
    assert body == {"status": "ok", "outbound_tls": "certifi"}

    unreadable = _fake_unreadable_bundle(tmp_path)
    monkeypatch.setattr(tls.certifi, "where", lambda: unreadable)
    monkeypatch.setattr(tls, "_FALLBACK_BUNDLES", (readable,))  # type: ignore[arg-type]
    body = asyncio.run(health())
    assert body["status"] == "ok"
    assert body["outbound_tls"].startswith(f"fallback:{readable}")

    monkeypatch.setattr(tls, "_FALLBACK_BUNDLES", ())  # type: ignore[arg-type]
    body = asyncio.run(health())
    assert body["status"] == "degraded"
    assert body["outbound_tls"].startswith("unreadable:")


def test_ourairports_wraps_tls_failure(monkeypatch: pytest.MonkeyPatch):
    """A certifi OSError becomes the actionable 502 envelope, not raw text."""
    from backend.airports import cache as cache_mod
    from backend.airports.ourairports import OurAirportsError, _fetch_csv

    async def cache_miss(key: str) -> str | None:  # noqa: ARG001
        return None

    def broken_client(**_kw):  # matches tls.async_client kwargs
        raise OSError(5, "Input/output error", "cacert.pem")

    monkeypatch.setattr(cache_mod, "cache_get", cache_miss)
    monkeypatch.setattr(tls, "async_client", broken_client)

    with pytest.raises(OurAirportsError) as exc_info:
        asyncio.run(_fetch_csv("airports"))
    assert "TLS CA bundle unreadable" in exc_info.value.detail
    assert "cacert.pem" in exc_info.value.detail
