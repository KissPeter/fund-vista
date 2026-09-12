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
from backend.penplot import labels
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
    methods = list(params.methods or ["hatch"])
    method_label = "+".join(methods)
    t0 = time.perf_counter()
    warnings: list[str] = []
    # Title strip first: reserve the label zone from the image area so
    # artwork bottoms out on the divider instead of sliding under it.
    # 0 when the label is off/blank/fully-unsupported (render no-ops).
    reserve_bottom_mm = 0.0
    if params.label.enabled and params.label.text.strip():
        reserve_bottom_mm = labels.label_reserve_mm(
            params.label.text,
            height_mm=params.label.height_mm,
            font=params.label.font,
            border=params.label.border,
        )
        if reserve_bottom_mm > 0.0:
            # +1 mm so image strokes can't linemerge into divider/frame.
            reserve_bottom_mm += labels.LABEL_ARTWORK_GAP_MM
    try:
        if is_vector:
            raw_px, vw, vh, svg_warnings = imaging.parse_svg_vectors(image_bytes)
            src_w, src_h = vw, vh
            warnings.extend(svg_warnings)
            _timed("parse-svg", image_id, method_label, t0)
            gray = None
            mask = None
        else:
            gray, w, h, _fmt = imaging.load_raster(image_bytes)
            src_w, src_h = float(w), float(h)
            _timed("load", image_id, method_label, t0)
            t0 = time.perf_counter()
            gray, scaled = imaging.maybe_downscale(gray, settings.max_image_dim_px)
            if scaled:
                warnings.append("image_downscaled_for_performance")
                src_w, src_h = float(gray.shape[1]), float(gray.shape[0])
            if params.remove_background:
                gray = imaging.remove_background(gray)
                warnings.append("background_removed")
            gray = imaging.blur(gray, params.blur_radius)
            gray = imaging.adjust_contrast(gray, params.contrast)
            gray = imaging.adjust_brightness(gray, params.brightness)
            mask = imaging.threshold_mask(gray, params.threshold)
            _timed("preprocess", image_id, method_label, t0)
            t0 = time.perf_counter()
            # Hatch pitch is authored in mm; convert to px with the same scale
            # layout() will later use, so WYSIWYG holds on the page.
            scale = layout_scale(
                src_w, src_h,
                params.page.size, params.page.orientation, params.page.margin_mm,
                reserve_bottom_mm,
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
                hatch_angle_deg=params.hatch_angle_deg,
                hatch_pitch_px=pitch_px_clamped,
            )
            # Every selected generator runs on the same (mask, gray) in the
            # requested order; outputs concatenate before the shared optimize
            # chain below. Listed order is preserved here (e.g. shading first,
            # outlines last) — linesort later only reorders for travel.
            raw_px = []
            for method in methods:
                generator = METHOD_REGISTRY[method]
                raw_px.extend(generator.generate(mask, gray, ctx))
                _timed(f"method-{method}", image_id, method_label, t0)

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
            reserve_bottom_mm=reserve_bottom_mm,
        )
        _timed("layout", image_id, method_label, t0)
        if params.label.enabled and params.label.text.strip():
            # Title-block label (mm space already): joins quantize and the
            # rest of the cleanup chain so it plots and counts like any stroke.
            lab_lines, lab_warnings = labels.render_label(
                params.label.text,
                height_mm=params.label.height_mm,
                align=params.label.align,
                page_w=page_w, page_h=page_h,
                margin_mm=params.page.margin_mm,
                font=params.label.font,
                border=params.label.border,
                pad_left_mm=params.label.pad_left_mm,
                pad_right_mm=params.label.pad_right_mm,
                border_radius_mm=params.label.border_radius_mm,
            )
            warnings.extend(lab_warnings)
            _timed("label", image_id, method_label, t0)
        else:
            lab_lines = []
        # Whole-page margin frame (independent of the label). Skipped when the
        # label border already draws the same rect — never double-ink it.
        frame_lines: list = []
        if params.page.frame and not (lab_lines and params.label.border):
            frame_lines = [labels.page_frame_rect(
                page_w, page_h,
                params.page.margin_mm, params.page.frame_radius_mm,
            )]
        t0 = time.perf_counter()
        # Merge domains separately: a joint linemerge would fuse image strokes
        # into divider/frame across the strip gap at high tolerances, dragging
        # artwork out of the image area (or the frame into it).
        q = settings.quantization_mm
        tol = params.linemerge_tolerance_mm
        merged = linemerge(quantize(laid, q), tol)
        static_lines = lab_lines + frame_lines
        if static_lines:
            merged.extend(linemerge(quantize(static_lines, q), tol))
        _timed("linemerge", image_id, method_label, t0)
        t0 = time.perf_counter()
        simplified = linesimplify(merged, params.linesimplify_tolerance_mm)
        _timed("linesimplify", image_id, method_label, t0)
        t0 = time.perf_counter()
        ordered = linesort(simplified) if params.linesort else simplified
        _timed("linesort", image_id, method_label, t0)
        final = reloop(ordered, params.reloop_tolerance_mm)
        _timed("reloop", image_id, method_label, t0)

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
        log.exception("pipeline.convert failed image=%s method=%s", image_id[:12], method_label)
        raise processing_failed() from exc
