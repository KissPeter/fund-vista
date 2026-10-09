"""Ownership / licence metadata stamped into generated SVGs (penplot.svgmeta)."""

from __future__ import annotations

import json

import cv2
import defusedxml.ElementTree as ET
import numpy as np
import pytest

from backend.penplot import imaging, svgmeta
from backend.penplot.config import Settings
from backend.penplot.pipeline import convert_result_filename, params_hash, run_convert
from backend.penplot.schemas import ConvertParams, SvgMetaParams

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="10mm" '
       'viewBox="0 0 10 10">\n<g fill="none" stroke="#000"><path d="M 1 1 L 9 9"/></g></svg>\n')
NS = {"svg": "http://www.w3.org/2000/svg", "dc": "http://purl.org/dc/elements/1.1/",
      "cc": "http://creativecommons.org/ns#",
      "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#"}
OWNER = dict(svg_creator="Pen Pixel Shop", svg_rights="(c) 2026 Pen Pixel Shop.",
             svg_license="cc-by-nc-4.0")


def _png() -> bytes:
    img = np.full((120, 160), 255, np.uint8)
    cv2.rectangle(img, (30, 30), (120, 90), 0, 2)
    return cv2.imencode(".png", img)[1].tobytes()


def _convert(params: ConvertParams, tmp_path, **settings_kw):
    data = _png()
    w, h, _ = imaging.probe_raster(data)
    return run_convert(image_id="c" * 64, image_bytes=data, is_vector=False,
                       src_w=float(w), src_h=float(h), params=params,
                       settings=Settings(data_dir=str(tmp_path), **settings_kw))


# ------------------------------------------------------------------- off by default

def test_nothing_configured_changes_nothing(tmp_path):
    assert svgmeta.stamp(SVG, Settings()) == SVG
    assert svgmeta.salt(Settings()) == ""
    res = _convert(ConvertParams(methods=["centerline"]), tmp_path)
    assert "<metadata" not in res.svg_text and "<title" not in res.svg_text
    # Legacy cache key/filename are untouched while unconfigured.
    assert params_hash(ConvertParams()) == "36fc02273dcf"
    assert convert_result_filename("a" * 64, ConvertParams(), False, Settings()).endswith(
        "_36fc02273dcf_optimized.svg")


# ------------------------------------------------------------------- the block

def test_stamped_svg_is_valid_xml_with_the_ownership_notice():
    out = svgmeta.stamp(SVG, Settings(**OWNER, svg_attribution_url="https://example.org/pp"),
                        title="Ikarus 412", description="Bus plan")
    root = ET.fromstring(out)
    assert root.find("svg:title", NS).text == "Ikarus 412"
    assert "Bus plan" in root.find("svg:desc", NS).text
    work = root.find("svg:metadata/rdf:RDF/cc:Work", NS)
    assert work.find("dc:creator/cc:Agent/dc:title", NS).text == "Pen Pixel Shop"
    assert "2026" in work.find("dc:rights/cc:Agent/dc:title", NS).text
    assert work.find("cc:license", NS).get(f"{{{NS['rdf']}}}resource").endswith("/by-nc/4.0/")
    assert work.find("cc:attributionURL", NS).get(f"{{{NS['rdf']}}}resource") == "https://example.org/pp"
    lic = root.find("svg:metadata/rdf:RDF/cc:License", NS)
    prohibits = [e.get(f"{{{NS['rdf']}}}resource") for e in lic.findall("cc:prohibits", NS)]
    assert prohibits == [svgmeta.CC_NS + "CommercialUse"]          # "no commercial use"
    # Geometry is untouched.
    assert root.find("svg:g/svg:path", NS).get("d") == "M 1 1 L 9 9"
    assert out.count("<path") == 1


def test_all_rights_reserved_has_no_cc_license_and_no_double_period():
    out = svgmeta.stamp(SVG, Settings(svg_creator="Studio", svg_rights="No use without permission.",
                                      svg_license="all-rights-reserved"))
    root = ET.fromstring(out)
    assert root.find("svg:metadata/rdf:RDF/cc:Work/cc:license", NS) is None
    desc = root.find("svg:desc", NS).text
    assert ".." not in desc and "All rights reserved" in desc


def test_nc_nd_and_share_alike_terms():
    nd = ET.fromstring(svgmeta.stamp(SVG, Settings(svg_creator="A", svg_license="cc-by-nc-nd-4.0")))
    names = [e.get(f"{{{NS['rdf']}}}resource").rsplit("#", 1)[1]
             for e in nd.findall("svg:metadata/rdf:RDF/cc:License/cc:permits", NS)]
    assert "DerivativeWorks" not in names and "Reproduction" in names
    sa = ET.fromstring(svgmeta.stamp(SVG, Settings(svg_creator="A", svg_license="cc-by-nc-sa-4.0")))
    reqs = [e.get(f"{{{NS['rdf']}}}resource").rsplit("#", 1)[1]
            for e in sa.findall("svg:metadata/rdf:RDF/cc:License/cc:requires", NS)]
    assert "ShareAlike" in reqs


def test_values_are_escaped_and_control_chars_stripped():
    nasty = 'A & B <script>alert("x")</script> ]]> \x00\x07 line\nbreak'
    out = svgmeta.stamp(SVG, Settings(svg_creator=nasty, svg_rights=nasty), title=nasty,
                        description=nasty)
    root = ET.fromstring(out)                       # still well-formed
    assert "<script>" not in out and "\x00" not in out
    title = root.find("svg:title", NS).text
    assert "<script>" in title and "\n" not in title  # text survives, as text
    assert len(svgmeta.clean("x" * 5000, 200)) == 200


def test_license_and_url_settings_fail_fast():
    for bad in ({"svg_license": "mit"}, {"svg_license_url": "javascript:alert(1)"},
                {"svg_attribution_url": "ftp://x"}, {"svg_license_url": "https://a b"}):
        with pytest.raises(ValueError):
            Settings(**bad)
    assert Settings(svg_license="CC-BY-NC-4.0").svg_license == "cc-by-nc-4.0"  # case-insensitive
    custom = svgmeta.stamp(SVG, Settings(svg_creator="A", svg_license_url="https://terms.example/x"))
    assert ET.fromstring(custom).find("svg:metadata/rdf:RDF/cc:Work/cc:license", NS).get(
        f"{{{NS['rdf']}}}resource") == "https://terms.example/x"


def test_stamping_is_idempotent_and_skips_non_svg():
    s = Settings(**OWNER)
    once = svgmeta.stamp(SVG, s)
    assert svgmeta.stamp(once, s) == once
    assert svgmeta.stamp("not svg at all", s) == "not svg at all"
    assert svgmeta.stamp(SVG.replace("<svg ", "<svg xmlns:dc='x' ", 1), s).count("xmlns:dc") == 1


def test_the_svg_parser_ignores_the_block():
    """Stamped SVGs are re-ingestable (wordmark/citymap -> convert)."""
    stamped = svgmeta.stamp(SVG, Settings(**OWNER), title="t")
    plain = imaging.parse_svg_vectors(SVG.encode())
    again = imaging.parse_svg_vectors(stamped.encode())
    assert plain[0] == again[0] and plain[1:3] == again[1:3]


# --------------------------------------------------------------- pipeline / cache

def test_convert_stamps_owner_and_request_title_and_keys_the_cache(tmp_path):
    base = ConvertParams(methods=["centerline"])
    plain = _convert(base, tmp_path)
    owned = _convert(base, tmp_path, **OWNER)
    titled = _convert(ConvertParams(methods=["centerline"],
                                    svg_meta=SvgMetaParams(title="My plan", description="D")),
                      tmp_path, **OWNER)
    root = ET.fromstring(owned.svg_text)
    assert root.find("svg:metadata/rdf:RDF/cc:Work/dc:creator/cc:Agent/dc:title", NS).text == "Pen Pixel Shop"
    assert ET.fromstring(titled.svg_text).find("svg:title", NS).text == "My plan"
    # Same geometry; only the notice differs.
    assert owned.svg_text.replace(svgmeta.build_block(svgmeta.resolve(Settings(**OWNER))), "").split("<g")[1] \
        == plain.svg_text.split("<g")[1].replace("</svg>", "</svg>")
    # Changing the server config can never serve a stale file: filenames differ.
    names = {convert_result_filename("a" * 64, base, False, Settings(**kw))
             for kw in ({}, OWNER, {**OWNER, "svg_license": "all-rights-reserved"})}
    assert len(names) == 3
    assert owned.filename != plain.filename


def test_a_client_cannot_override_ownership():
    with pytest.raises(ValueError):                       # no creator/rights/licence fields
        ConvertParams(svg_meta={"creator": "Someone else"})
    with pytest.raises(ValueError):
        SvgMetaParams(title="x" * 201)
    assert params_hash(ConvertParams(svg_meta={"title": ""})) == "36fc02273dcf"  # empty = unchanged key
    assert params_hash(ConvertParams(svg_meta={"title": "t"})) != "36fc02273dcf"


def test_wordmark_svg_is_stamped_too(monkeypatch):
    from backend.wordmark.geometry import render_wordmark

    plain = render_wordmark("AB", "poppins-bold", "solid", -0.12, 0.04, None, 600)[0]
    assert "<metadata" not in plain
    monkeypatch.setenv("PENPLOT_SVG_CREATOR", "Pen Pixel Shop")
    monkeypatch.setenv("PENPLOT_SVG_LICENSE", "cc-by-nc-4.0")
    stamped = render_wordmark("AB", "poppins-bold", "solid", -0.12, 0.04, None, 600)[0]
    root = ET.fromstring(stamped)
    assert root.find("svg:metadata/rdf:RDF/cc:Work/dc:creator/cc:Agent/dc:title", NS).text == "Pen Pixel Shop"
    assert root.find("svg:path", NS) is not None


# -------------------------------------------------------------------------- CLI

def test_cli_stamps_owner_flags_and_defaults_the_title_to_the_file_name(tmp_path):
    from backend.penplot import img2plot

    src = tmp_path / "bus plan.png"
    src.write_bytes(_png())
    rc = img2plot.main([str(src), "-o", str(tmp_path / "o.svg"), "--no-ocr", "-q",
                        "--creator", "Pen Pixel Shop", "--license", "cc-by-nc-4.0",
                        "--rights", "(c) 2026 Pen Pixel Shop.", "--description", "Plan"])
    assert rc == img2plot.EXIT_OK
    root = ET.fromstring((tmp_path / "o.svg").read_text())
    assert root.find("svg:title", NS).text == "bus plan"
    assert "Plan" in root.find("svg:desc", NS).text
    assert root.find("svg:metadata/rdf:RDF/cc:Work/cc:license", NS) is not None
    # --no-title drops the file-name title but keeps the ownership block.
    rc = img2plot.main([str(src), "-o", str(tmp_path / "n.svg"), "--no-ocr", "-q", "--no-title",
                        "--creator", "Pen Pixel Shop"])
    nt = ET.fromstring((tmp_path / "n.svg").read_text())
    assert nt.find("svg:title", NS) is None and nt.find("svg:metadata", NS) is not None
    # A bad licence is a usage error (exit 2) before any image is touched.
    with pytest.raises(SystemExit) as exc:
        img2plot.main([str(src), "-o", str(tmp_path / "x.svg"), "--license", "mit"])
    assert exc.value.code == 2 and not (tmp_path / "x.svg").exists()
    # Batch: every image gets its own title and the same owner.
    (tmp_path / "scans").mkdir()
    for n in ("a.png", "b.png"):
        (tmp_path / "scans" / n).write_bytes(_png())
    img2plot.main([str(tmp_path / "scans"), "--out-dir", str(tmp_path / "out"), "--no-ocr", "-q",
                   "--creator", "Pen Pixel Shop", "--report", str(tmp_path / "r.json")])
    titles = {p.name: ET.fromstring(p.read_text()).find("svg:title", NS).text
              for p in (tmp_path / "out").glob("*.svg")}
    assert titles == {"a.svg": "a", "b.svg": "b"}
    assert json.loads((tmp_path / "r.json").read_text())["summary"]["ok"] == 2
