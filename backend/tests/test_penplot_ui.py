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
        'id="m_contour"',
        'id="m_hatch"',
        'id="m_flow"',
        'id="hatch_angle_deg"',
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
    # Sliders + reset button (id must not shadow form.reset — see below).
    assert 'type="range"' in html
    assert 'id="resetBtn"' in html
    assert 'id="threshold_val"' in html
    assert 'id="contrast"' in html
    assert 'id="brightness"' in html
    assert 'id="remove_background"' in html
    assert 'id="label_border_radius_mm"' in html
    # Label fieldset: toggle + text + align + height + font + border.
    for marker in ('id="label_enabled"', 'id="label_text"', 'id="label_align"',
                   'id="label_height_mm"', 'id="label_font"',
                   'id="label_border"', 'id="label_pad_left_mm"',
                   'id="label_pad_right_mm"',
                   'id="label_border_radius_mm"',
                   'value="fill"', 'value="futural"',
                   'value="futuram"', 'value="simplex"'):
        assert marker in html, marker
    # Page frame controls.
    for marker in ('id="page_frame"', 'id="page_frame_radius_mm"'):
        assert marker in html, marker
    # Busy lock: controls disable mid-flight, coalesced follow-up after.
    assert "setBusy" in html and "pending" in html
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


def test_ui_no_control_shadows_form_builtins(http_client):
    """No control inside #params may be id/name'd reset/submit/...: named
    controls override HTMLFormElement built-ins, so e.g. id="reset" turns
    form.reset() into the button element and silently kills Reset."""
    import re

    html = http_client.get("/penplot").text
    form = re.search(r'<form id="params".*?</form>', html, re.DOTALL).group(0)
    ids = set(re.findall(r'id="([^"]+)"', form))
    names = set(re.findall(r'name="([^"]+)"', form))
    shadowers = {
        "reset", "submit", "action", "method", "elements", "length",
        "name", "target", "encoding", "enctype",
    }
    assert not (ids & shadowers), ids & shadowers
    assert not (names & shadowers), names & shadowers
    # And the reset handler must call the real form.reset().
    assert '$("params").reset()' in html
