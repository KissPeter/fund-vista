"""The uploaded Gerber is kept with its render and served behind the design token.

``GET /v1/pcb/results/{design_id}/source`` returns the original bytes. The
route needs the design token as ``exp`` and ``sig`` query parameters.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend.penplot import tokens as design_tokens
from backend.tests.helpers import TEST_HMAC_SECRET

GBR = (Path(__file__).parent / "fixtures" / "pcb_sublayers.gbr").read_bytes()
RENDER_PARAMS = {
    "paper": "white",
    "pen": "black",
    "layers": {
        "pads": {"enabled": True, "ink": "auto"},
        "tracks": {"enabled": False, "ink": "auto"},
        "pours": {"enabled": False, "ink": "auto"},
        "drills": {"enabled": False, "ink": "auto"},
    },
    "mirror": False,
}


def _render(http_client) -> dict:
    resp = http_client.post(
        "/v1/pcb/render",
        files={"file": ("board.gbr", GBR, "application/octet-stream")},
        data={"params": json.dumps(RENDER_PARAMS)},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _source_url(design_id: str) -> str:
    return f"/v1/pcb/results/{design_id}/source"


def test_source_returns_the_uploaded_bytes(http_client):
    design = _render(http_client)["design"]
    resp = http_client.get(_source_url(design["design_id"]), params={
        "exp": design["exp"], "sig": design["sig"],
    })
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/octet-stream")
    assert resp.content == GBR


def test_source_with_a_bad_signature_is_403(http_client):
    design = _render(http_client)["design"]
    resp = http_client.get(_source_url(design["design_id"]), params={
        "exp": design["exp"], "sig": "00" * 32,
    })
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "token_invalid"


def test_source_with_an_expired_token_is_403(http_client):
    design_id = "ef" * 32
    exp = int(time.time()) - 60
    sig = design_tokens.sign_design_token(
        design_id=design_id, exp=exp, secret=TEST_HMAC_SECRET,
    )
    resp = http_client.get(_source_url(design_id), params={"exp": exp, "sig": sig})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "token_invalid"


def test_source_for_an_unknown_design_is_404(http_client):
    design_id = "cd" * 32
    exp = int(time.time()) + 3600
    sig = design_tokens.sign_design_token(
        design_id=design_id, exp=exp, secret=TEST_HMAC_SECRET,
    )
    resp = http_client.get(_source_url(design_id), params={"exp": exp, "sig": sig})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "result_not_found"


def test_source_with_a_malformed_exp_is_403(http_client):
    design = _render(http_client)["design"]
    resp = http_client.get(_source_url(design["design_id"]), params={
        "exp": "soon", "sig": design["sig"],
    })
    assert resp.status_code == 403


SPLIT_URL = "/v1/fulfill/ink-split"


@pytest.fixture
def local_client(monkeypatch):
    """The app in this process, so a test can drop cache keys it reads.

    The live server runs in a subprocess and cannot see a monkeypatch; the
    signing secret is set on both routers that issue and check tokens.
    """
    from fastapi.testclient import TestClient

    from backend.fulfillment import router as fulfill_router
    from backend.main import app
    from backend.pcb import router as pcb_router

    for module in (pcb_router, fulfill_router):
        monkeypatch.setattr(
            module, "_settings",
            module._settings.model_copy(update={"hmac_secret": TEST_HMAC_SECRET}),
        )
    with TestClient(app) as client:
        yield client


def _split(client, design: dict):
    return client.post(SPLIT_URL, json={
        "design": {"design_id": design["design_id"], "exp": design["exp"], "sig": design["sig"]},
        "kind": "pcb",
        "params": RENDER_PARAMS,
    })


def _drop_keys(monkeypatch, dropped: set[str]):
    """Make the named cache keys look missing to the readers in this process."""
    from backend.pcb import cache as pcb_cache

    real_get_many = pcb_cache.cache_get_many

    async def without(keys):
        found = await real_get_many(keys)
        return {key: value for key, value in found.items() if key not in dropped}

    monkeypatch.setattr(pcb_cache, "cache_get_many", without)


def test_ink_split_rebuilds_a_missing_sheet_from_the_stored_source(local_client, monkeypatch):
    from backend.pcb import cache as pcb_cache

    design = _render(local_client)["design"]
    _drop_keys(monkeypatch, {pcb_cache.svg_cache_key(design["design_id"])})
    resp = _split(local_client, design)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["design_id"] == design["design_id"]
    assert [f["ink"] for f in body["files"]] == ["black"]
    assert body["cache_hit"] is False


def test_ink_split_is_draft_expired_when_sheet_and_source_are_gone(local_client, monkeypatch):
    from backend.pcb import cache as pcb_cache

    design = _render(local_client)["design"]
    dead = {
        pcb_cache.svg_cache_key(design["design_id"]),
        pcb_cache.source_cache_key(design["design_id"]),
    }
    _drop_keys(monkeypatch, dead)
    resp = _split(local_client, design)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "draft_expired"
