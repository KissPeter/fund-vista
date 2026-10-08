"""CLI: image (URL or file) -> plotter SVG, via the same pipeline as /penplot.

    python -m backend.penplot.img2plot <url|path> -o out.svg

Defaults are ``drawing.DRAWING_PRESET`` (the /penplot "Technical drawing"
preset), and the conversion is ``pipeline.run_convert`` itself, so a result
produced here can be reproduced on the page by pressing that preset button.
Text step: the ``tesserocr`` wheel (Linux, bundled engine) or, elsewhere, the
``tesseract`` binary (macOS: ``brew install tesseract``); without either the
step is skipped with an ``ocr_unavailable`` warning.
"""

from __future__ import annotations

import argparse
import hashlib
import sys

import httpx

from backend.penplot import drawing, imaging
from backend.penplot.config import Settings
from backend.penplot.pipeline import run_convert
from backend.penplot.schemas import ConvertParams


def load_bytes(src: str) -> bytes:
    if src.startswith(("http://", "https://")):
        r = httpx.get(src, follow_redirects=True, timeout=30,
                      headers={"User-Agent": "pen-pixel-shop-img2plot/0.2"})
        r.raise_for_status()
        return r.content
    with open(src, "rb") as fh:
        return fh.read()


def build_params(a: argparse.Namespace) -> ConvertParams:
    """Preset + CLI overrides -> validated ``ConvertParams``."""
    data = {k: (dict(v) if isinstance(v, dict) else v)
            for k, v in drawing.DRAWING_PRESET.items()}
    for flag, key in (("threshold", "threshold"), ("contrast", "contrast"),
                      ("brightness", "brightness"), ("blur", "blur_radius"),
                      ("prune_px", "centerline_prune_px"), ("simplify", "contour_simplify"),
                      ("upscale", "trace_upscale"), ("curve_smooth", "curve_smooth"),
                      ("linemerge_mm", "linemerge_tolerance_mm"),
                      ("linesimplify_mm", "linesimplify_tolerance_mm")):
        if getattr(a, flag) is not None:
            data[key] = getattr(a, flag)
    ocr = data["ocr_text"]
    ocr["enabled"] = not a.no_ocr
    for flag in ("min_conf", "min_chars"):
        if getattr(a, flag) is not None:
            ocr[flag] = getattr(a, flag)
    data["circles"] = not a.no_circles
    for flag, key in (("size", "size"), ("orientation", "orientation"),
                      ("margin_mm", "margin_mm")):
        if getattr(a, flag) is not None:
            data["page"][key] = getattr(a, flag)
    return ConvertParams(**data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("src")
    ap.add_argument("-o", "--out", default="out.svg")
    for flag, typ, help_ in (
        ("threshold", int, "0-255"), ("contrast", float, "0-3, 1 = identity"),
        ("brightness", float, "-100..100"), ("blur", float, "blur_radius, 0-10"),
        ("prune-px", int, "centerline_prune_px"), ("simplify", float, "contour_simplify"),
        ("upscale", int, "trace_upscale 1-3"), ("curve-smooth", int, "Chaikin passes 0-3"),
        ("linemerge-mm", float, ""), ("linesimplify-mm", float, ""),
        ("min-conf", float, "OCR reading agreement %, 5-100"),
        ("min-chars", int, "shortest OCR reading accepted"),
        ("size", str, "A4/A3/A5/Letter"), ("orientation", str, "portrait|landscape"),
        ("margin-mm", float, ""),
    ):
        ap.add_argument(f"--{flag}", type=typ, default=None, help=help_)
    ap.add_argument("--no-ocr", action="store_true", help="skip text recognition")
    ap.add_argument("--no-circles", action="store_true", help="skip circle detection")
    a = ap.parse_args(argv)

    data = load_bytes(a.src)
    width, height, _fmt = imaging.probe_raster(data)
    params = build_params(a)
    result = run_convert(
        image_id=hashlib.sha256(data).hexdigest(), image_bytes=data,
        is_vector=False, src_w=float(width), src_h=float(height),
        params=params, settings=Settings())
    with open(a.out, "w") as fh:
        fh.write(result.svg_text)
    print(f"wrote {a.out}: {result.stats.strokes} strokes, "
          f"warnings={result.warnings}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
