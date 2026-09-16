"""Shop work order (pen-pixel docs/poc/python-service-changes.md) over real HTTP.

Covers §1 (CORS preflight from the shop origin), §2 (token mint + verify,
including an independent HMAC test vector), §3 (absolute svg_url), §4a
(retain promotes to the 90-day TTL), and §6 (/v1/health).

Unit tests pin the token algorithm without a server; live tests assert the
wiring (endpoints, envelopes, CORS headers).
"""

from __future__ import annotations

import hashlib
import hmac
import time

from backend.penplot import tokens as design_tokens
from backend.tests.helpers import (
    TEST_HMAC_SECRET,
    default_params,
    png_bytes,
    upload,
)

SHOP_ORIGIN = "https://penplot.linuxadm.hu"
# Static GitHub Pages frontend (VITE_BACKEND_BASE_URL points here in prod).
PAGES_ORIGIN = "https://kisspeter.github.io"


# -- §2 algorithm (no server) ---------------------------------------------


def test_token_algorithm_matches_contract_vector():
    # Independent reimplementation of the contract formula:
    # sig = hex(HMAC_SHA256(secret, design_id + "|" + exp)).
    design_id = "a" * 64
    exp = 1730000000
    expected = hmac.new(
        TEST_HMAC_SECRET.encode(), f"{design_id}|{exp}".encode(), hashlib.sha256
    ).hexdigest()
    assert design_tokens.sign_design_token(
        design_id=design_id, exp=exp, secret=TEST_HMAC_SECRET
    ) == expected


def test_token_verify_roundtrip_and_failures():
    design_id = "b" * 64
    exp = int(time.time()) + 3600
    sig = design_tokens.sign_design_token(
        design_id=design_id, exp=exp, secret=TEST_HMAC_SECRET
    )
    assert design_tokens.verify_design_token(
        design_id=design_id, exp=exp, sig=sig, secret=TEST_HMAC_SECRET
    ) == (True, "ok")
    # Tampered payload fails.
    assert design_tokens.verify_design_token(
        design_id="c" * 64, exp=exp, sig=sig, secret=TEST_HMAC_SECRET
    )[0] is False
    # Expired token reports expired, not bad_signature.
    old_exp = int(time.time()) - 10
    old_sig = design_tokens.sign_design_token(
        design_id=design_id, exp=old_exp, secret=TEST_HMAC_SECRET
    )
    assert design_tokens.verify_design_token(
        design_id=design_id, exp=old_exp, sig=old_sig, secret=TEST_HMAC_SECRET
    ) == (False, "expired")
    # Rotation window: previous secret accepted with its own reason.
    assert design_tokens.verify_design_token(
        design_id=design_id, exp=exp, sig=sig,
        secret="new-secret", previous_secret=TEST_HMAC_SECRET,
    ) == (True, "ok_previous_secret")
    # Unknown secret fails.
    assert design_tokens.verify_design_token(
        design_id=design_id, exp=exp, sig=sig, secret="wrong"
    ) == (False, "bad_signature")


# -- live HTTP -------------------------------------------------------------


def test_cors_preflight_passes_from_shop_origin(http_client):
    resp = http_client.options(
        "/v1/images",
        headers={
            "Origin": SHOP_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["access-control-allow-origin"] == SHOP_ORIGIN


def test_cors_allows_shop_get(http_client):
    resp = http_client.get("/v1/health", headers={"Origin": SHOP_ORIGIN})
    assert resp.status_code == 200, resp.text
    assert resp.headers["access-control-allow-origin"] == SHOP_ORIGIN


def test_cors_preflight_passes_from_github_pages(http_client):
    # The static frontend at kisspeter.github.io/fund-vista/ calls this API
    # cross-origin; covered by the allow_origin_regex default, not the list.
    resp = http_client.options(
        "/v1/images",
        headers={
            "Origin": PAGES_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["access-control-allow-origin"] == PAGES_ORIGIN


def test_token_mint_verify_roundtrip(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    mint = http_client.post("/v1/tokens", json={"image_id": image_id})
    assert mint.status_code == 200, mint.text
    body = mint.json()
    assert body["design_id"] == image_id
    # TTL 24 h, minute-accurate.
    assert abs(body["exp"] - (int(time.time()) + 24 * 3600)) < 120
    expected_sig = hmac.new(
        TEST_HMAC_SECRET.encode(),
        f"{image_id}|{body['exp']}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert body["sig"] == expected_sig
    verify = http_client.get(
        "/v1/tokens/verify",
        params={"design_id": image_id, "exp": body["exp"], "sig": body["sig"]},
    )
    assert verify.status_code == 200, verify.text
    assert verify.json() == {
        "design_id": image_id,
        "exp": body["exp"],
        "valid": True,
        "reason": "ok",
    }


def test_token_unknown_image_404_and_bad_sig_invalid(http_client):
    mint = http_client.post("/v1/tokens", json={"image_id": "0" * 64})
    assert mint.status_code == 404
    assert mint.json()["error"]["code"] == "image_not_found"
    verify = http_client.get(
        "/v1/tokens/verify",
        params={"design_id": "0" * 64, "exp": int(time.time()) + 60, "sig": "f" * 64},
    )
    assert verify.status_code == 200
    assert verify.json()["valid"] is False


def test_convert_svg_url_is_absolute(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    resp = http_client.post(
        "/v1/convert", json={"image_id": image_id, "params": default_params("hatch")}
    )
    assert resp.status_code == 200, resp.text
    svg_url = resp.json()["svg_url"]
    assert svg_url.startswith("http://127.0.0.1:"), svg_url
    assert "/v1/results/" in svg_url


def test_retain_promotes_to_90_day_ttl(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    meta_before = http_client.get(f"/v1/images/{image_id}").json()
    retain = http_client.post(f"/v1/images/{image_id}/retain")
    assert retain.status_code == 200, retain.text
    body = retain.json()
    assert body == {
        "image_id": image_id,
        "retained": True,
        "expires_at": body["expires_at"],
    }
    # Anonymous TTL is 48 h; retained must be ~90 d from now.
    from datetime import datetime, timezone

    expires = datetime.fromisoformat(body["expires_at"])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    ttl_h = (expires - datetime.now(tz=timezone.utc)).total_seconds() / 3600
    assert 2150 < ttl_h <= 2160, ttl_h
    assert meta_before["expires_at"] != body["expires_at"]
    # Idempotent: second call extends nothing and still 200s.
    assert http_client.post(f"/v1/images/{image_id}/retain").status_code == 200
    # Unknown id 404s with the standard envelope.
    missing = http_client.post("/v1/images/" + "0" * 64 + "/retain")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "image_not_found"


def test_health_reports_dependencies(http_client):
    resp = http_client.get("/v1/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] in ("ok", "degraded")
    assert body["redis"] in ("reachable", "unreachable", "unconfigured")
    assert body["disk_free_mb"] > 0
    assert body["store_writable"] is True


# -- CORS env formats (2026-09-16: dashboards store the list as JSON) --------


def test_cors_env_accepts_json_array_format():
    from backend.main import Settings

    assert Settings._split_origins(
        '["https://kisspeter.github.io", "https://penplot.linuxadm.hu"]'
    ) == ["https://kisspeter.github.io", "https://penplot.linuxadm.hu"]


def test_cors_env_still_accepts_comma_separated():
    from backend.main import Settings

    assert Settings._split_origins("http://localhost:8080,https://penplot.linuxadm.hu") == [
        "http://localhost:8080",
        "https://penplot.linuxadm.hu",
    ]


def test_cors_env_json_array_reaches_settings(monkeypatch):
    from backend.main import Settings

    monkeypatch.setenv(
        "CORS_ALLOW_ORIGINS",
        '["https://kisspeter.github.io", "https://penplot.linuxadm.hu"]',
    )
    assert Settings().cors_allow_origins == [
        "https://kisspeter.github.io",
        "https://penplot.linuxadm.hu",
    ]
