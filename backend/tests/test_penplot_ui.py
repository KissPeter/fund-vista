"""Server-rendered UI page (backend/penplot/ui.py + templates/penplot.html).

Asserts the page serves over real HTTP ahead of the catch-all proxy and that
the rendered form matches the code (defaults from ConvertParams, method/page
options, endpoint URLs the inline JS calls).
"""

from __future__ import annotations

from backend.penplot.schemas import ConvertParams


def test_ui_page_serves_html(http_client):
    resp = http_client.get("/penplot")
    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers["content-type"]
    html = resp.text
    # Form controls the JS wires up.
    for marker in (
        'id="file"',
        'id="params"',
        'id="method"',
        'id="threshold"',
        'id="hatch_pitch_mm"',
        'id="linesort"',
        'id="page_size"',
        'id="preview"',
        'id="stats"',
        'id="warnings"',
        'id="vpype"',
    ):
        assert marker in html, marker
    # Inline JS talks to the versioned JSON endpoints (incl. re-upload retry).
    assert "/v1/images" in html
    assert "/v1/convert" in html
    assert "image_not_found" in html
    # All three methods are offered.
    for method in ("contour", "hatch", "flow"):
        assert f'value="{method}"' in html, method


def test_ui_defaults_match_schema(http_client):
    """Server-rendered defaults must equal ConvertParams defaults."""
    defaults = ConvertParams().model_dump(mode="json")
    html = http_client.get("/penplot").text
    assert f'value="{defaults["threshold"]}"' in html
    assert f'value="{defaults["hatch_pitch_mm"]}"' in html
    assert f'value="{defaults["pen"]["draw_speed_mm_s"]}"' in html
    assert "A4" in html and "A3" in html
