"""GET /v1/results/{filename}/meta — stats the shop prices a design from."""

from __future__ import annotations

import pytest

from backend.tests.helpers import default_params, png_bytes, upload


def _filename(svg_url: str) -> str:
    return svg_url.rsplit("/", 1)[-1]


def test_meta_returns_the_convert_stats(http_client):
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    conv = http_client.post(
        "/v1/convert", json={"image_id": image_id, "params": default_params("hatch")}
    )
    assert conv.status_code == 200
    body = conv.json()
    meta = http_client.get(f"/v1/results/{_filename(body['svg_url'])}/meta")
    assert meta.status_code == 200
    assert meta.json()["stats"] == body["stats"]
    assert "pen_down_mm" in meta.json()["stats"]
    assert "estimated_time_s" in meta.json()["stats"]


@pytest.mark.parametrize(
    "filename",
    [
        "nope_optimized.svg",
        "../secret_optimized.svg",
        "notasvg.txt",
    ],
)
def test_meta_unknown_or_hostile_filenames_are_404(http_client, filename):
    assert http_client.get(f"/v1/results/{filename}/meta").status_code == 404
