"""Download-only visible credit: backend-native domain stamp (issue #55).

Preview bytes stay clean and deterministic; ``GET /results/{file}?download=1``
(or ``?credit=1``) applies :func:`svgmeta.stamp_download` at serve time, never
stored. Complements the shop-side guard (pen-pixel-shop#935), which passes
already-stamped bytes through unchanged.
"""

from __future__ import annotations

import re

import defusedxml.ElementTree as ET

from backend.penplot import imaging, svgmeta
from backend.penplot.config import Settings
from backend.tests.helpers import default_params, png_bytes, upload

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="210.00mm" '
       'height="297.00mm" viewBox="0 0 210.000 297.000">\n'
       '<g fill="none" stroke="#000"><path d="M 1 1 L 9 9"/></g></svg>\n')
NS = {"svg": "http://www.w3.org/2000/svg"}
OWNER = {"svg_creator": "Pen Pixel Shop", "svg_rights": "(c) 2026 Pen Pixel Shop.",
         "svg_license": "cc-by-nc-4.0",
         "svg_attribution_url": "https://penplot.linuxadm.hu/shop"}


def _settings(**kw) -> Settings:
    return Settings(**kw)


def _paths(text: str) -> list[str]:
    return re.findall(r'<path\b[^>]*\bd="([^"]+)"', text)


# ------------------------------------------------------- domain + detection


def test_credit_domain_uses_attribution_host():
    assert svgmeta.credit_domain(_settings(**OWNER)) == "penplot.linuxadm.hu"
    assert svgmeta.credit_domain(Settings()) == ""
    assert svgmeta.credit_domain(
        _settings(svg_attribution_url="https://Example.ORG:8443/a?b=c")) == "example.org"


def test_has_visible_credit_detects_own_shop_and_generic_markers():
    s = _settings(**OWNER)
    assert not svgmeta.has_visible_credit(SVG)
    assert svgmeta.has_visible_credit(svgmeta.stamp_download(SVG, s))
    assert svgmeta.has_visible_credit(SVG.replace(
        "</svg>", '<g id="penplot-credit"><text>x</text></g></svg>'))
    assert svgmeta.has_visible_credit(SVG.replace(
        "</svg>", '<g data-credit="1"><text>x</text></g></svg>'))
    assert svgmeta.has_visible_credit(SVG.replace(
        "</svg>", '<g id="svg-credit-line"><text>penplot.linuxadm.hu</text></g></svg>'))
    assert svgmeta.has_visible_credit(SVG + "<!-- penplot.linuxadm.hu -->")


# ------------------------------------------------------------- visible stamp


def test_visible_credit_off_unless_attribution_configured():
    assert svgmeta.stamp_visible_credit(SVG, Settings()) == SVG
    only_creator = svgmeta.stamp_visible_credit(
        SVG, _settings(svg_creator="Studio"))
    assert only_creator == SVG  # domain (not creator) drives the visible line


def test_visible_credit_is_viewbox_aware_centred_and_valid_xml():
    out = svgmeta.stamp_visible_credit(SVG, _settings(**OWNER))
    root = ET.fromstring(out)
    el = root.find("svg:g[@id='penplot-credit']/svg:text", NS)
    assert el is not None and el.text == "penplot.linuxadm.hu"
    assert el.get("text-anchor") == "middle"
    assert abs(float(el.get("x")) - 105.0) < 1e-6  # minx + w/2
    y = float(el.get("y"))
    assert 287.0 < y < 297.0  # bottom out-of-border margin, never the artwork
    assert 2.5 <= float(el.get("font-size")) <= 4.0
    assert _paths(out) == _paths(SVG)  # geometry untouched
    assert out.count("<path") == 1


def test_visible_credit_idempotent_and_deterministic():
    s = _settings(**OWNER)
    once = svgmeta.stamp_visible_credit(SVG, s)
    assert svgmeta.stamp_visible_credit(once, s) == once
    assert svgmeta.stamp_visible_credit(SVG, s) == svgmeta.stamp_visible_credit(SVG, s)
    # Shop-stamped input is a no-op (no second line).
    shop = SVG.replace("</svg>",
                       '<g id="svg-credit"><text>penplot.linuxadm.hu</text></g></svg>')
    assert svgmeta.stamp_visible_credit(shop, s) == shop


def test_visible_credit_skips_unplaceable_svg():
    s = _settings(**OWNER)
    assert svgmeta.stamp_visible_credit("not svg at all", s) == "not svg at all"
    assert svgmeta.stamp_visible_credit("<svg></svg>", s) == "<svg></svg>"


# ------------------------------------------------------------ download stamp


def test_download_carries_metadata_plus_visible_line():
    s = _settings(**OWNER)
    preview = svgmeta.stamp(SVG, s)
    assert "penplot-credit" not in preview  # preview stays clean
    download = svgmeta.stamp_download(SVG, s)
    root = ET.fromstring(download)
    assert root.find("svg:metadata", NS) is not None
    assert root.find("svg:g[@id='penplot-credit']/svg:text", NS).text == "penplot.linuxadm.hu"
    assert _paths(download) == _paths(SVG)


def test_download_idempotent_and_deterministic():
    s = _settings(**OWNER)
    once = svgmeta.stamp_download(SVG, s)
    assert svgmeta.stamp_download(once, s) == once
    assert svgmeta.stamp_download(SVG, s) == svgmeta.stamp_download(SVG, s)
    # Metadata-only input still gains exactly one visible line.
    meta_only = svgmeta.stamp(SVG, s)
    assert svgmeta.stamp_download(meta_only, s) == once


def test_download_off_means_byte_identical_preview():
    assert svgmeta.stamp_download(SVG, Settings()) == SVG


def test_parser_ignores_visible_credit_on_reingest():
    s = _settings(**OWNER)
    stamped = svgmeta.stamp_download(SVG, s)
    plain = imaging.parse_svg_vectors(SVG.encode())
    again = imaging.parse_svg_vectors(stamped.encode())
    assert plain[0] == again[0] and plain[1:3] == again[1:3]


def test_parser_ignores_generic_credit_id():
    shop = SVG.replace("</svg>",
                       '<g id="svgCredit"><text x="105" y="294">penplot.linuxadm.hu</text></g></svg>')
    plain = imaging.parse_svg_vectors(SVG.encode())
    again = imaging.parse_svg_vectors(shop.encode())
    assert plain[0] == again[0]


# ------------------------------------------------------------------- HTTP


def _convert_ok(http_client) -> dict:
    image_id = upload(http_client, png_bytes()).json()["image_id"]
    resp = http_client.post(
        "/v1/convert", json={"image_id": image_id, "params": default_params("contour")})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_results_preview_is_clean_and_download_param_is_noop_unconfigured(http_client):
    body = _convert_ok(http_client)
    preview = http_client.get(body["svg_url"])
    assert preview.status_code == 200
    assert "penplot-credit" not in preview.text
    assert "<metadata" not in preview.text  # live server has no SVG_* configured
    download = http_client.get(body["svg_url"] + "?download=1")
    assert download.status_code == 200
    assert download.text == preview.text  # off unless configured: byte-identical
    assert "attachment" in download.headers.get("content-disposition", "")
    assert 'filename="' in download.headers.get("content-disposition", "")
    credit = http_client.get(body["svg_url"] + "?credit=1")
    assert credit.status_code == 200 and credit.text == preview.text
    assert "content-disposition" not in {k.lower() for k in credit.headers}
    # Re-requesting is stable.
    assert http_client.get(body["svg_url"] + "?download=1").text == download.text


def test_results_download_404s_match_preview(http_client):
    assert http_client.get("/v1/results/does_not_exist_optimized.svg").status_code == 404
    assert http_client.get(
        "/v1/results/does_not_exist_optimized.svg?download=1").status_code == 404
    assert http_client.get(
        "/v1/results/does_not_exist_optimized.svg?credit=1").status_code == 404
