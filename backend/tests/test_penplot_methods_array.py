"""Multi-method converts: `methods` array + legacy `method` alias.

The array runs every selected generator in order and concatenates before the
shared optimize chain; the singular `method` maps to a one-element array and
is cleared, so both spellings share one result cache entry.
"""

from __future__ import annotations

from backend.tests.helpers import default_params, png_bytes, upload


def _convert(http_client, image_id: str, params: dict):
    return http_client.post("/v1/convert", json={"image_id": image_id, "params": params})


def test_methods_array_merges_generators(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = default_params()
    del params["method"]
    params["methods"] = ["hatch", "contour"]
    resp = _convert(http_client, image_id, params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stats"]["strokes"] >= 1
    assert body["stats"]["points"]["after"] <= body["stats"]["points"]["before"]
    # A different method set addresses a different (deterministic) file…
    solo = _convert(http_client, image_id, default_params("hatch")).json()
    assert body["svg_url"] != solo["svg_url"]
    # …and repeating the same array hits the cache.
    again = _convert(http_client, image_id, params).json()
    assert again["svg_url"] == body["svg_url"]
    svg = http_client.get(body["svg_url"])
    assert svg.status_code == 200 and "<path" in svg.text


def test_legacy_method_alias_shares_cache_with_array(http_client):
    """`method: hatch` and `methods: [hatch]` canonicalize identically."""
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    via_alias = _convert(http_client, image_id, default_params("hatch")).json()
    params = default_params()
    del params["method"]
    params["methods"] = ["hatch"]
    via_array = _convert(http_client, image_id, params).json()
    assert via_alias["svg_url"] == via_array["svg_url"]


def test_methods_deduped_preserving_order(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = default_params()
    del params["method"]
    params["methods"] = ["contour", "hatch", "hatch"]
    first = _convert(http_client, image_id, params).json()
    params["methods"] = ["contour", "hatch"]
    second = _convert(http_client, image_id, params).json()
    assert first["svg_url"] == second["svg_url"]


def test_method_and_methods_together_422(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = default_params("hatch")  # keeps legacy "method"
    params["methods"] = ["contour"]
    resp = _convert(http_client, image_id, params)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "invalid_params"


def test_empty_methods_422(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = default_params()
    del params["method"]
    params["methods"] = []
    resp = _convert(http_client, image_id, params)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "invalid_params"


def test_unknown_method_in_array_422(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = default_params()
    del params["method"]
    params["methods"] = ["hatch", "crayon"]
    resp = _convert(http_client, image_id, params)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "invalid_params"


def test_hatch_angle_changes_output(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = default_params("hatch")
    params["hatch_angle_deg"] = 90.0
    tilted = _convert(http_client, image_id, params)
    assert tilted.status_code == 200, tilted.text
    straight = _convert(http_client, image_id, default_params("hatch")).json()
    assert tilted.json()["svg_url"] != straight["svg_url"]
    # Same angle twice is deterministic.
    again = _convert(http_client, image_id, params).json()
    assert again["svg_url"] == tilted.json()["svg_url"]


def test_hatch_angle_out_of_range_422(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    for bad in (180.0, -5.0):
        params = default_params("hatch")
        params["hatch_angle_deg"] = bad
        resp = _convert(http_client, image_id, params)
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json()["error"]["code"] == "invalid_params"


def test_contrast_neutral_is_identity(http_client):
    """Explicit contrast=1.0 must hit the same cache entry as the default."""
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    default = _convert(http_client, image_id, default_params("hatch")).json()
    params = default_params("hatch")
    params["contrast"] = 1.0
    explicit = _convert(http_client, image_id, params).json()
    assert explicit["svg_url"] == default["svg_url"]


def test_contrast_boost_changes_output(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    params = default_params("hatch")
    params["contrast"] = 2.5
    boosted = _convert(http_client, image_id, params)
    assert boosted.status_code == 200, boosted.text
    plain = _convert(http_client, image_id, default_params("hatch")).json()
    assert boosted.json()["svg_url"] != plain["svg_url"]


def test_contrast_out_of_range_422(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    for bad in (-0.5, 4.5):
        params = default_params("hatch")
        params["contrast"] = bad
        resp = _convert(http_client, image_id, params)
        assert resp.status_code == 422, (bad, resp.text)
        assert resp.json()["error"]["code"] == "invalid_params"
