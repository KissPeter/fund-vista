"""CLI: image(s) (URL, file or folder) -> plotter SVG, via the same pipeline as /penplot.

    python -m backend.penplot.img2plot drawing.jpg -o drawing.svg
    python -m backend.penplot.img2plot scans/ --out-dir plots/ --report plots/report.json

Defaults are ``drawing.DRAWING_PRESET`` (the /penplot "Technical drawing"
preset), and every conversion is ``pipeline.run_convert`` itself, so a result
produced here can be reproduced on the page by pressing that preset button.
Text step: the ``tesserocr`` wheel (Linux, bundled engine) or, elsewhere, the
``tesseract`` binary (macOS: ``brew install tesseract``); without either the
step is skipped with an ``ocr_unavailable`` warning.

Batch use: several inputs (or a folder) need ``--out-dir``; each image becomes
``<stem>.svg``. ``--report`` writes one JSON record per image, ``--skip-existing``
makes reruns resumable, ``--jobs N`` runs N images at once, and the exit code is
non-zero if any image failed. Full manual: docs/img2plot-cli.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from urllib.parse import unquote, urlparse

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def is_url(src: str) -> bool:
    return src.startswith(("http://", "https://"))


def load_bytes(src: str) -> bytes:
    if is_url(src):
        import httpx  # noqa: PLC0415

        r = httpx.get(src, follow_redirects=True, timeout=30,
                      headers={"User-Agent": "pen-pixel-shop-img2plot/0.3"})
        r.raise_for_status()
        return r.content
    with open(src, "rb") as fh:
        return fh.read()


def expand_sources(srcs: list[str]) -> list[str]:
    """Files and URLs as given; a folder becomes its images (sorted, not recursive)."""
    out: list[str] = []
    for src in srcs:
        if not is_url(src) and os.path.isdir(src):
            out.extend(
                os.path.join(src, name) for name in sorted(os.listdir(src))
                if name.lower().endswith(IMAGE_EXTS))
        else:
            out.append(src)
    return out


def stem_of(src: str) -> str:
    path = unquote(urlparse(src).path) if is_url(src) else src
    return os.path.splitext(os.path.basename(path.rstrip("/")))[0] or "image"


def plan_outputs(srcs: list[str], out_dir: str) -> list[tuple[str, str]]:
    """(src, out_path) pairs; two inputs with the same stem never overwrite each other."""
    seen: dict[str, int] = {}
    plan = []
    for src in srcs:
        stem = stem_of(src)
        seen[stem] = seen.get(stem, 0) + 1
        if seen[stem] > 1:
            stem = f"{stem}-{hashlib.sha1(src.encode()).hexdigest()[:6]}"
        plan.append((src, os.path.join(out_dir, f"{stem}.svg")))
    return plan


def build_params(a: argparse.Namespace):
    """Preset + CLI overrides -> validated ``ConvertParams``."""
    from backend.penplot import drawing  # noqa: PLC0415
    from backend.penplot.schemas import ConvertParams  # noqa: PLC0415

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
    data["strip_frame"] = not a.keep_frame
    for flag, key in (("size", "size"), ("orientation", "orientation"),
                      ("margin_mm", "margin_mm"), ("frame_radius_mm", "frame_radius_mm"),
                      ("padding_mm", "padding_mm")):
        if getattr(a, flag) is not None:
            data["page"][key] = getattr(a, flag)
    if a.frame:
        data["page"]["frame"] = True
    if a.label is not None:
        data["label"] = {"enabled": True, "text": a.label,
                         **({"font": a.label_font} if a.label_font else {})}
    return ConvertParams(**data)


def process_one(src: str, out: str, argd: dict) -> dict:
    """Convert one image; never raises (a batch must survive a bad file).

    Top-level and picklable so ``--jobs`` can run it in worker processes.
    Returns the report record: ``status`` ok|error, ``src``, ``out``,
    ``strokes``, ``seconds``, ``warnings`` and, on failure, ``error``.
    """
    started = time.perf_counter()
    rec: dict = {"src": src, "out": out, "status": "error"}
    try:
        from backend.penplot import imaging  # noqa: PLC0415
        from backend.penplot.config import Settings  # noqa: PLC0415
        from backend.penplot.pipeline import run_convert  # noqa: PLC0415

        data = load_bytes(src)
        width, height, _fmt = imaging.probe_raster(data)
        result = run_convert(
            image_id=hashlib.sha256(data).hexdigest(), image_bytes=data,
            is_vector=False, src_w=float(width), src_h=float(height),
            params=build_params(argparse.Namespace(**argd)), settings=Settings())
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        tmp = f"{out}.tmp"
        with open(tmp, "w") as fh:
            fh.write(result.svg_text)
        os.replace(tmp, out)  # a killed run never leaves a half-written SVG
        rec.update(status="ok", strokes=result.stats.strokes,
                   warnings=list(result.warnings))
    except Exception as exc:  # noqa: BLE001 - report, keep going
        rec["error"] = f"{type(exc).__name__}: {exc}"
    rec["seconds"] = round(time.perf_counter() - started, 2)
    return rec


def make_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", nargs="+", help="image file, URL or folder (repeatable)")
    ap.add_argument("-o", "--out", help="output SVG (single input only)")
    ap.add_argument("--out-dir", help="output folder (required for several inputs)")
    ap.add_argument("--report", help="write a JSON report (one record per image)")
    ap.add_argument("--jobs", type=int, default=1, help="images converted at once (default 1)")
    ap.add_argument("--skip-existing", action="store_true",
                    help="skip images whose output exists and is newer than the input")
    ap.add_argument("--fail-fast", action="store_true", help="stop at the first failure")
    ap.add_argument("-q", "--quiet", action="store_true", help="no per-image progress lines")
    for flag, typ, help_ in (
        ("threshold", int, "0-255"), ("contrast", float, "0-3, 1 = identity"),
        ("brightness", float, "-100..100"), ("blur", float, "blur_radius, 0-10"),
        ("prune-px", int, "centerline_prune_px"), ("simplify", float, "contour_simplify"),
        ("upscale", int, "trace_upscale 1-3"), ("curve-smooth", int, "Chaikin passes 0-3"),
        ("linemerge-mm", float, ""), ("linesimplify-mm", float, ""),
        ("min-conf", float, "OCR reading agreement %, 5-100"),
        ("min-chars", int, "shortest OCR reading accepted"),
        ("size", str, "A4/A3/A5/Letter"), ("orientation", str, "portrait|landscape"),
        ("margin-mm", float, ""), ("padding-mm", float, "inner padding inside the margin"),
        ("frame-radius-mm", float, "corner radius of the page frame (with --frame)"),
        ("label", str, "title-block text (enables the label strip)"),
        ("label-font", str, "label font, default znikoslsvginot"),
    ):
        ap.add_argument(f"--{flag}", type=typ, default=None, help=help_)
    ap.add_argument("--no-ocr", action="store_true", help="skip text recognition")
    ap.add_argument("--no-circles", action="store_true", help="skip circle detection")
    ap.add_argument("--frame", action="store_true",
                    help="draw the project's own page frame (see --frame-radius-mm)")
    ap.add_argument("--keep-frame", action="store_true",
                    help="keep the drawing's own border (default: detect and remove it)")
    return ap


def _line(i: int, n: int, rec: dict) -> str:
    name = os.path.basename(rec["out"])
    if rec["status"] == "ok":
        warn = f" warnings={rec['warnings']}" if rec["warnings"] else ""
        return f"[{i}/{n}] ok    {name}  {rec['strokes']} strokes  {rec['seconds']}s{warn}"
    return f"[{i}/{n}] FAIL  {os.path.basename(rec['src'])}  {rec['error']}"


def main(argv=None) -> int:
    ap = make_parser()
    a = ap.parse_args(argv)
    # Batch runs have no HTTP timeout: wait for the OCR/trace slots instead of
    # degrading, and give OCR a generous budget. Explicit PENPLOT_* env wins.
    os.environ.setdefault("PENPLOT_OCR_QUEUE_S", "3600")
    os.environ.setdefault("PENPLOT_OCR_BUDGET_S", "120")

    srcs = expand_sources(a.src)
    if not srcs:
        ap.error("no images found")
    if a.jobs < 1:
        ap.error("--jobs must be >= 1")
    if len(srcs) == 1 and a.out and not a.out_dir:
        plan = [(srcs[0], a.out)]
    elif a.out_dir:
        plan = plan_outputs(srcs, a.out_dir)
    elif len(srcs) == 1:
        plan = [(srcs[0], f"{stem_of(srcs[0])}.svg")]
    else:
        ap.error("several inputs need --out-dir")
    try:
        build_params(a)  # fail on bad flag values before touching any image
    except ValueError as exc:  # pydantic.ValidationError is a ValueError
        ap.error("invalid option value: " + str(exc).replace("\n", " "))

    argd = vars(a)
    todo = []
    records: list[dict] = []
    for src, out in plan:
        if (a.skip_existing and not is_url(src) and os.path.exists(out)
                and os.path.getmtime(out) >= os.path.getmtime(src)):
            records.append({"src": src, "out": out, "status": "skipped",
                            "strokes": None, "seconds": 0.0, "warnings": []})
        else:
            todo.append((src, out))

    n, done = len(todo), 0

    def finish(rec: dict) -> bool:
        nonlocal done
        done += 1
        records.append(rec)
        if not a.quiet:
            print(_line(done, n, rec), file=sys.stderr, flush=True)
        return rec["status"] == "ok" or not a.fail_fast

    t0 = time.perf_counter()
    if a.jobs == 1 or n <= 1:
        for src, out in todo:
            if not finish(process_one(src, out, argd)):
                break
    else:
        with ProcessPoolExecutor(max_workers=min(a.jobs, n)) as pool:
            futures = [pool.submit(process_one, s, o, argd) for s, o in todo]
            for fut in as_completed(futures):
                if not finish(fut.result()):
                    for f in futures:
                        f.cancel()
                    break

    failed = [r for r in records if r["status"] == "error"]
    skipped = sum(r["status"] == "skipped" for r in records)
    summary = {"total": len(plan), "ok": sum(r["status"] == "ok" for r in records),
               "failed": len(failed), "skipped": skipped,
               "seconds": round(time.perf_counter() - t0, 1)}
    if a.report:
        os.makedirs(os.path.dirname(os.path.abspath(a.report)), exist_ok=True)
        with open(a.report, "w") as fh:
            json.dump({"summary": summary, "results": sorted(records, key=lambda r: r["src"])},
                      fh, indent=2)
    print(f"{summary['ok']} ok, {summary['failed']} failed, {skipped} skipped "
          f"of {summary['total']} in {summary['seconds']}s", file=sys.stderr)
    return EXIT_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
