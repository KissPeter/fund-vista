"""GET /v1/version, X-Backend-Version header, /healthz stamp, fallbacks."""

from __future__ import annotations

import pytest

from backend import version


@pytest.fixture(autouse=True)
def _fresh_cache():
    version.backend_version.cache_clear()
    yield
    version.backend_version.cache_clear()


def test_env_wins_and_full_sha_is_shortened(monkeypatch):
    monkeypatch.setenv("FUNDVISTA_VERSION", "a" * 40)
    assert version.backend_version() == "a" * 7


def test_falls_back_to_git_then_dev(monkeypatch):
    monkeypatch.delenv("FUNDVISTA_VERSION", raising=False)
    sha = version.backend_version()
    assert sha == "dev" or len(sha) == 7
    version.backend_version.cache_clear()
    monkeypatch.setattr(version, "_git_sha", lambda: None)
    assert version.backend_version() == "dev"


def test_endpoint_header_and_healthz(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.main import app

    http_client = TestClient(app)
    monkeypatch.setenv("FUNDVISTA_VERSION", "abc1234")
    monkeypatch.setenv("FUNDVISTA_BUILT", "2026-10-30T12:00:00Z")
    resp = http_client.get("/v1/version")
    assert resp.status_code == 200
    assert resp.json() == {"backend": "abc1234", "built": "2026-10-30T12:00:00Z"}
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-backend-version"] == "abc1234"
    other = http_client.get("/v1/health")
    assert other.headers["x-backend-version"] == "abc1234"
    assert http_client.get("/healthz").json()["backend"] == "abc1234"
    assert "x-backend-version" not in http_client.get("/healthz").headers


def test_built_is_null_when_unset(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.main import app

    http_client = TestClient(app)
    monkeypatch.delenv("FUNDVISTA_BUILT", raising=False)
    assert http_client.get("/v1/version").json()["built"] is None
