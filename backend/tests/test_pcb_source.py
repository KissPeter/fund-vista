"""The uploaded Gerber is kept with its render and served behind the design token.

``GET /v1/pcb/results/{design_id}/source`` returns the original bytes. The
route needs the design token as ``exp`` and ``sig`` query parameters.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

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
