"""Orchestration for POST /v1/convert: pixels -> lines -> optimized SVG + stats.

Each stage is timed and debug-logged (``pipeline.convert image=... method=..
stage=... ms=..``) so a slow slider value can be traced to hatch/flow vs.
linemerge without a profiler. Pure function of (image bytes, params) apart
from the result cache — same input always yields byte-identical SVG.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

import numpy as np

from backend.penplot import imaging
from backend.penplot.config import Settings
from backend.penplot.errors import PenPlotError, processing_failed
from backend.penplot.methods import METHOD_REGISTRY, MethodContext
from backend.penplot.optimize import (
    build_vpype_command,
    count_points,
    layout,
    layout_scale,
    linemerge,
    linesimplify,
    linesort,
    polyline_length,
    dist,
    quantize,
    reloop,
    to_svg,
)
from backend.penplot.schemas import ConvertParams, ConvertStats, PointsStats, SegmentsStats

log = logging.getLogger(__name__)


class ConvertResult:
    def __init__(
        self,
        *,
        svg_text: str,
        filename: str,
        stats: ConvertStats,
        warnings: list[str],
        vpype_command: str,
    ) -> None:
        self.svg_text = svg_text
        self.filename = filename
        self.stats = stats
        self.warnings = warnings
        self.vpype_command = vpype_command


def params_hash(params: ConvertParams) -> str:
    canonical = json.dumps(params.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _timed(label: str, image_id: str, method: str, started: float) -> None:
    log.debug(
        "pipeline.convert image=%s method=%s stage=%s ms=%.1f",
        image_id[:12], method, label, (time.perf_counter() - started) * 1000.0,
    )


def run_convert(
    *,
    image_id: str,
    image_bytes: bytes,
    is_vector: bool,
    src_w: float,
    src_h: float,
    params: ConvertParams,
    settings: Settings,
) -> ConvertResult:
    method = params.method
    t0 = time.perf_counter()
    warnings: list[str] = []
    try:
        if is_vector:
            raw_px, vw, vh, svg_warnings = imaging.parse_svg_vectors(image_bytes)
            src_w, src_h = vw, vh
            warnings.extend(svg_warnings)
            _timed("parse-svg", image_id, method, t0)
            gray = None
            mask = None
        else:
            gray, w, h, _fmt = imaging.load_raster(image_bytes)
            src_w, src_h = float(w), float(h)
            _timed("load", image_id, method, t0)
            t0 = time.perf_counter()
            gray, scaled = imaging.maybe_downscale(gray, settings.max_image_dim_px)
            if scaled:
                warnings.append("image_downscaled_for_performance")
                src_w, src_h = float(gray.shape[1]), float(gray.shape[0])
            gray = imaging.blur(gray, params.blur_radius)
            mask = imaging.threshold_mask(gray, params.threshold)
            _timed("preprocess", image_id, method, t0)
            t0 = time.perf_counter()
            # Hatch pitch is authored in mm; convert to px with the same scale
            # layout() will later use, so WYSIWYG holds on the page.
            scale = layout_scale(
                src_w, src_h,
                params.page.size, params.page.orientation, params.page.margin_mm,
            )
            pitch_px = params.hatch_pitch_mm / max(scale, 1e-9)
            pitch_px_clamped = float(np.clip(pitch_px, 2.0, 200.0))
            if pitch_px != pitch_px_clamped:
                # Extreme page/image combos can request a sub-satisfiable pitch;
                # surface the silent clamp instead of just doing it (C.2.4a).
                warnings.append("hatch_pitch_clamped")
            ctx = MethodContext(
                threshold=params.threshold,
                blur_radius=params.blur_radius,
                hatch_pitch_mm=params.hatch_pitch_mm,
                contour_simplify=params.contour_simplify,
                hatch_pitch_px=pitch_px_clamped,
            )
            generator = METHOD_REGISTRY[method]
            raw_px = generator.generate(mask, gray, ctx)
            _timed(f"method-{method}", image_id, method, t0)

        points_before = count_points(raw_px)
        segments_before = len(raw_px)

        t0 = time.perf_counter()
        # Layout FIRST (px -> mm) so tolerances behave exactly like vpype's
        # post-layout units; then merge/simplify/sort/reloop in mm.
        laid, page_w, page_h = layout(
            raw_px, src_w, src_h,
            size=params.page.size,
            orientation=params.page.orientation,
            margin_mm=params.page.margin_mm,
        )
        _timed("layout", image_id, method, t0)
        t0 = time.perf_counter()
        merged = linemerge(quantize(laid, settings.quantization_mm), params.linemerge_tolerance_mm)
        _timed("linemerge", image_id, method, t0)
        t0 = time.perf_counter()
        simplified = linesimplify(merged, params.linesimplify_tolerance_mm)
        _timed("linesimplify", image_id, method, t0)
        t0 = time.perf_counter()
        ordered = linesort(simplified) if params.linesort else simplified
        _timed("linesort", image_id, method, t0)
        final = reloop(ordered, params.reloop_tolerance_mm)
        _timed("reloop", image_id, method, t0)

        pen_down = sum(polyline_length(pl) for pl in final)
        pen_up = sum(
            dist(final[i][-1], final[i + 1][0]) for i in range(len(final) - 1)
        ) if len(final) > 1 else 0.0
        strokes = len(final)
        est = (
            pen_down / params.pen.draw_speed_mm_s
            + pen_up / params.pen.travel_speed_mm_s
            + strokes * params.pen.pen_lift_s
        )
        stats = ConvertStats(
            points=PointsStats(before=points_before, after=count_points(final)),
            segments=SegmentsStats(before=segments_before, after=len(final)),
            strokes=strokes,
            pen_down_mm=round(pen_down, 2),
            pen_up_mm=round(pen_up, 2),
            estimated_time_s=round(est, 1),
        )
        svg_text = to_svg(final, page_w, page_h)
        vpype_command = build_vpype_command(
            linemerge_tol=params.linemerge_tolerance_mm,
            linesimplify_tol=params.linesimplify_tolerance_mm,
            linesort_on=params.linesort,
            reloop_tol=params.reloop_tolerance_mm,
            page_size=params.page.size.upper(),
            margin_mm=params.page.margin_mm,
        )
        filename = f"{image_id}_{params_hash(params)}_optimized.svg"
        return ConvertResult(
            svg_text=svg_text, filename=filename, stats=stats,
            warnings=warnings, vpype_command=vpype_command,
        )
    except PenPlotError:
        raise
    except Exception as exc:
        log.exception("pipeline.convert failed image=%s method=%s", image_id[:12], method)
        raise processing_failed() from exc
