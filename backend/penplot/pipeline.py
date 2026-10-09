"""Orchestration for POST /v1/convert: pixels -> lines -> optimized SVG + stats.

Each stage is timed and debug-logged (``pipeline.convert image=... method=..
stage=... ms=..``) so a slow slider value can be traced to hatch/flow vs.
linemerge without a profiler. Pure function of (image bytes, params) apart
from the result cache — same input always yields byte-identical SVG.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time

import numpy as np

from backend.penplot import drawing
from backend.penplot import svgmeta
from backend.penplot import imaging
from backend.penplot import labels
from backend.penplot import stats_table
from backend.penplot.backgrounds import get_background_data_uri
from backend.penplot.config import Settings
from backend.penplot.errors import PenPlotError, processing_failed
from backend.penplot.knockout import knock_out
from backend.penplot.methods import METHOD_REGISTRY, MethodContext
from backend.penplot.optimize import (
    build_vpype_command,
    count_points,
    curvesmooth,
    densify,
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
from backend.penplot.svgparsecache import load_parsed_svg, store_parsed_svg

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


# Parameters that never reach the geometry for vector inputs: the
# technical-drawing-mode stages stay raster-only (they analyse photo
# pixels — OCR text, exact circles, thin-line keeping, supersampled
# tracing, frame cropping — and have no FE sliders).
_VECTOR_IGNORED_PARAMS = frozenset({
    "thin_lines",
    "trace_upscale",
    "circles",
    "ocr_text",
    "strip_frame",
})
# Tone + trace-tuning sliders. They shape vector geometry only on the
# rasterized path (see _vector_needs_raster): with a pure-contour method
# set vectors trace directly and there are no pixels to tone-map, so these
# stay out of the contour-only cache key instead of fragmenting it.
_VECTOR_TONE_PARAMS = frozenset({
    "threshold",
    "blur_radius",
    "contrast",
    "brightness",
    "remove_background",
    "strip_hatch_px",
    "hatch_pitch_mm",
    "hatch_angle_deg",
    "contour_simplify",
    "centerline_prune_px",
})
# Method generators that need fills: centerlines skeletons and hatch/flow
# shadings only exist on raster ink, so selecting any of them rasterizes
# the vector first (supersampled, antialiased). Plain contour traces the
# parsed polylines directly — byte-identical, fast, and the default
# everywhere (lineart preset, all designer tabs).
_VECTOR_RASTER_METHODS = frozenset({"centerline", "hatch", "flow"})


def _vector_needs_raster(methods: list[str] | None) -> bool:
    """True when the selected generators need raster ink.

    Mirrors run_convert's ``methods or ["hatch"]`` default: an empty set
    means hatch, which needs the raster path.
    """
    return any(m in _VECTOR_RASTER_METHODS for m in (methods or ["hatch"]))
# Default-off stages for the fast vector preview path (see P2 / gh #27).
_SKIPPABLE_TRAVEL_STAGES = frozenset({"linesort", "reloop_tolerance_mm"})


def is_fast_preview(params: ConvertParams, is_vector: bool) -> bool:
    """Vector inputs skip linesort/reloop unless ``full_quality`` is set.

    Those two stages only reorder strokes for pen-travel speed — the drawn
    geometry (what the preview shows) is byte-identical — and they are pure
    CPU on dense SVGs, so the fast preview path (the default) omits them.
    Raster inputs always optimise travel (their defaults already did).
    """
    return is_vector and not params.full_quality


def effective_params_dump(params: ConvertParams, is_vector: bool) -> dict:
    """Canonical param dict as actually executed (drives signature + cache key)."""
    data = params.model_dump(mode="json", exclude_none=True)
    # Drawing-stage params at their defaults are dropped so cache keys (and
    # stored result filenames) from before they existed stay valid.
    if not data.get("ocr_text", {}).get("enabled"):
        data.pop("ocr_text", None)
    if not any(data.get("svg_meta", {}).values()):
        data.pop("svg_meta", None)
    for field, default in (("thin_lines", False), ("circles", False),
                           ("trace_upscale", 1), ("strip_frame", False)):
        if data.get(field) == default:
            data.pop(field, None)
    if is_vector:
        for field in _VECTOR_IGNORED_PARAMS:
            data.pop(field, None)
        if not _vector_needs_raster(data.get("methods")):
            # Pure-contour vectors trace directly: no pixels exist to
            # tone-map, so the tone sliders stay out of the key instead of
            # fragmenting the result cache.
            for field in _VECTOR_TONE_PARAMS:
                data.pop(field, None)
        if not params.full_quality:
            for field in _SKIPPABLE_TRAVEL_STAGES:
                data.pop(field, None)
    return data


def params_hash(params: ConvertParams, is_vector: bool = False, salt: str = "") -> str:
    canonical = json.dumps(effective_params_dump(params, is_vector), sort_keys=True)
    if salt:  # server-side ownership config (svgmeta.salt); "" keeps legacy keys
        canonical += "|" + salt
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def convert_result_filename(
    image_id: str, params: ConvertParams, is_vector: bool,
    settings: Settings | None = None,
) -> str:
    return f"{image_id}_{params_hash(params, is_vector, svgmeta.salt(settings))}_optimized.svg"


def _timed(label: str, image_id: str, method: str, started: float) -> None:
    log.debug(
        "pipeline.convert image=%s method=%s stage=%s ms=%.1f",
        image_id[:12], method, label, (time.perf_counter() - started) * 1000.0,
    )


def _tone_to_mask(gray: np.ndarray, params: ConvertParams) -> tuple[np.ndarray, np.ndarray]:
    """Legacy tone stack: blur → contrast → brightness → threshold mask.

    Shared by the raster non-drawing path and the vector path (which feeds
    rasterized polylines), so a slider value tunes identically for both
    inputs.
    """
    gray = imaging.blur(gray, params.blur_radius)
    gray = imaging.adjust_contrast(gray, params.contrast)
    gray = imaging.adjust_brightness(gray, params.brightness)
    return gray, imaging.threshold_mask(gray, params.threshold)


def _generate_from_mask(
    *, params: ConvertParams, mask: np.ndarray, gray: np.ndarray, up: int,
    src_w: float, src_h: float, reserve_bottom_mm: float,
    image_id: str, method_label: str, methods: list[str],
    warnings: list[str], poll, started: float,
) -> list:
    """Shared method stage: hatch-strip → pitch/clamp → MethodContext →
    every selected generator in order.

    Runs on raster masks and on rasterized vectors alike, so the Style rail
    means the same thing for both inputs. ``poll`` is the caller's P2
    cancel checkpoint; ``started`` seeds the "preprocess" timing bucket.
    """
    if params.strip_hatch_px > 0:
        # Morphological opening on the ink mask: erases anything
        # thinner than strip_hatch_px (hatch/cross-hatch strokes)
        # while regenerating thicker strokes (outlines, solid fills)
        # at full width. Deliberately grouped into the "preprocess"
        # timing bucket below, same as blur/contrast/threshold.
        mask = imaging.strip_hatch(mask, params.strip_hatch_px * up)
        warnings.append("hatch_stripped")
    _timed("preprocess", image_id, method_label, started)
    # Hatch pitch is authored in mm; convert to px with the same scale
    # layout() will later use, so WYSIWYG holds on the page.
    scale = layout_scale(
        src_w, src_h,
        params.page.size, params.page.orientation, params.page.margin_mm,
        reserve_bottom_mm,
        params.page.padding_mm,
    )
    pitch_px = params.hatch_pitch_mm / max(scale, 1e-9)
    pitch_px_clamped = float(np.clip(pitch_px, 2.0, 200.0))
    if pitch_px != pitch_px_clamped:
        # Extreme page/image combos can request a sub-satisfiable pitch;
        # surface the silent clamp instead of just doing it (C.2.4a).
        warnings.append("hatch_pitch_clamped")
    # Pixel-valued knobs follow the supersampling factor ``up`` (1 on
    # the legacy path), so a value tuned at 1x means the same thing.
    ctx = MethodContext(
        threshold=params.threshold,
        blur_radius=params.blur_radius * up,
        hatch_pitch_mm=params.hatch_pitch_mm,
        contour_simplify=params.contour_simplify * up,
        hatch_angle_deg=params.hatch_angle_deg,
        hatch_pitch_px=pitch_px_clamped * up,
        centerline_prune_px=params.centerline_prune_px * up,
    )
    # Every selected generator runs on the same (mask, gray) in the
    # requested order; outputs concatenate before the shared optimize
    # chain below. Listed order is preserved here (e.g. shading first,
    # outlines last) — linesort later only reorders for travel.
    raw_px = []
    t0 = time.perf_counter()
    for method in methods:
        poll(f"vpype:{method}")
        generator = METHOD_REGISTRY[method]
        raw_px.extend(generator.generate(mask, gray, ctx))
        _timed(f"method-{method}", image_id, method_label, t0)
    if up > 1:  # back to working-image pixels
        raw_px = [[(x / up, y / up) for x, y in line] for line in raw_px]
    return raw_px


def run_convert(
    *,
    image_id: str,
    image_bytes: bytes,
    is_vector: bool,
    src_w: float,
    src_h: float,
    params: ConvertParams,
    settings: Settings,
    cancelled: object = None,
) -> ConvertResult:
    """Convert an image to plotter SVG.

    ``cancelled`` is an optional ``() -> bool`` polled between stages — after
    decode, before each method pass (``contour``/``centerline``/``hatch``/
    ``flow``), and between each vpype stage (``linemerge → linesimplify →
    linesort → reloop``) — i.e. P2 checkpoint 4 for converts. When it fires,
    :class:`backend.cancel.ClientCancelled` is raised; callers must skip
    result writes so no partial ``svg_url`` file is stored.
    """
    methods = list(params.methods or ["hatch"])
    method_label = "+".join(methods)
    t0 = time.perf_counter()
    warnings: list[str] = []

    def _poll(stage: str) -> None:
        # P2 checkpoint 4 inside the convert CPU loop.
        if cancelled is not None and callable(cancelled):
            try:
                if cancelled():  # type: ignore[operator]
                    from backend.cancel import ClientCancelled

                    raise ClientCancelled("/v1/convert", stage)
            except Exception as exc:
                from backend.cancel import ClientCancelled as _CC

                if isinstance(exc, _CC):
                    raise
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
    extra_px: list = []   # OCR text + detected circles (drawing mode), working px
    drawing_mode = (not is_vector) and drawing.is_drawing_mode(params)
    stack = contextlib.ExitStack()   # holds the heavy-trace slot in drawing mode
    up = 1                # trace supersampling factor actually used
    try:
        if is_vector:
            _poll("vpype:decode")
            parsed = load_parsed_svg(
                image_id, settings.parsed_dir, settings.parsed_svg_ttl_hours
            )
            if parsed is None:
                raw_px, vw, vh, svg_warnings = imaging.parse_svg_vectors(image_bytes)
                store_parsed_svg(
                    image_id, settings.parsed_dir, settings.parsed_svg_ttl_hours,
                    raw_px, vw, vh, svg_warnings,
                )
            else:
                raw_px, vw, vh, svg_warnings = parsed
            src_w, src_h = vw, vh
            warnings.extend(svg_warnings)
            _timed("parse-svg", image_id, method_label, t0)
            if not _vector_needs_raster(methods):
                # Plain contour: the parsed polylines already ARE the plot
                # geometry — trace them directly (byte-identical, fast).
                # Tone sliders have no pixels to act on here.
                gray = None
                mask = None
            else:
                # Centerline/hatch/flow need raster ink: rasterize the parsed
                # polylines (supersampled, antialiased) and run the same
                # tone+method pipeline as rasters, so every Style-rail
                # slider — threshold, blur, contrast, hatch pitch, simplify,
                # prune — shapes this geometry too.
                _poll("vector:rasterize")
                gray = imaging.rasterize_polylines(raw_px, vw, vh, settings.max_image_dim_px)
                src_w, src_h = float(gray.shape[1]), float(gray.shape[0])
                if params.remove_background:
                    gray = imaging.remove_background(gray)
                    warnings.append("background_removed")
                _timed("rasterize", image_id, method_label, t0)
                t0 = time.perf_counter()
                gray, mask = _tone_to_mask(gray, params)
                raw_px = _generate_from_mask(
                    params=params, mask=mask, gray=gray, up=1,
                    src_w=src_w, src_h=src_h, reserve_bottom_mm=reserve_bottom_mm,
                    image_id=image_id, method_label=method_label, methods=methods,
                    warnings=warnings, poll=_poll, started=t0,
                )
                del mask, gray
        else:
            _poll("vpype:decode")
            gray, w, h, _fmt = imaging.load_raster(image_bytes)
            src_w, src_h = float(w), float(h)
            _timed("load", image_id, method_label, t0)
            t0 = time.perf_counter()
            gray, scaled = imaging.maybe_downscale(gray, settings.max_image_dim_px)
            if scaled:
                warnings.append("image_downscaled_for_performance")
                src_w, src_h = float(gray.shape[1]), float(gray.shape[0])
            if params.strip_frame:
                # Before everything else: all later coordinates (OCR boxes,
                # circles, layout scale) live in the cropped image's space.
                gray, frame_warnings = drawing.strip_frame(gray)
                warnings.extend(frame_warnings)
                src_w, src_h = float(gray.shape[1]), float(gray.shape[0])
            if params.remove_background:
                gray = imaging.remove_background(gray)
                warnings.append("background_removed")
            # Technical-drawing mode (all opt-in; off = the legacy path below,
            # byte-identical): tone first, then OCR text / circles are lifted
            # out of the raster, then the ink mask is built (optionally
            # supersampled, thin lines kept). See ``drawing``.
            trace_upscale = params.trace_upscale
            if drawing_mode:
                if not stack.enter_context(drawing.trace_slot(settings, cancelled)):
                    # Another heavy trace holds the memory budget: do this one at
                    # 1x rather than risk an out-of-memory kill.
                    trace_upscale = 1
                    warnings.append(drawing.WARNING_TRACE_BUSY)
                tone = imaging.adjust_brightness(
                    imaging.adjust_contrast(gray, params.contrast), params.brightness)
                if params.circles or params.ocr_text.enabled:
                    _poll("drawing:analyse")
                    analysis = drawing.analyse(
                        tone, ocr=params.ocr_text, circles=params.circles,
                        settings=settings, image_id=image_id,
                        # Word boxes are in this image's pixel space, so the
                        # (possibly frame-cropped) size is part of the key.
                        tone_args=(params.contrast, params.brightness,
                                   params.remove_background, tone.shape[0], tone.shape[1]),
                        cancelled=cancelled)
                    tone = analysis.cleaned
                    extra_px = analysis.text_px + analysis.circle_px
                    warnings.extend(analysis.warnings)
                    _timed("drawing", image_id, method_label, t0)
                    t0 = time.perf_counter()
                gray, mask, up, ink_warnings = drawing.build_ink(
                    tone, threshold=params.threshold, blur=params.blur_radius,
                    thin_lines=params.thin_lines, upscale=trace_upscale,
                    max_pixels=settings.trace_max_pixels)
                warnings.extend(ink_warnings)
            else:
                gray, mask = _tone_to_mask(gray, params)
            raw_px = _generate_from_mask(
                params=params, mask=mask, gray=gray, up=up,
                src_w=src_w, src_h=src_h, reserve_bottom_mm=reserve_bottom_mm,
                image_id=image_id, method_label=method_label, methods=methods,
                warnings=warnings, poll=_poll, started=t0,
            )
            del mask, gray
            stack.close()  # arrays freed; let the next heavy trace in

        points_before = count_points(raw_px) + count_points(extra_px)
        segments_before = len(raw_px) + len(extra_px)

        t0 = time.perf_counter()
        # Layout FIRST (px -> mm) so tolerances behave exactly like vpype's
        # post-layout units; then merge/simplify/sort/reloop in mm.
        laid, page_w, page_h = layout(
            raw_px, src_w, src_h,
            size=params.page.size,
            orientation=params.page.orientation,
            margin_mm=params.page.margin_mm,
            reserve_bottom_mm=reserve_bottom_mm,
            padding_mm=params.page.padding_mm,
        )
        extra_laid: list = []
        if extra_px:
            # Same px -> mm transform as the artwork, so text/circles register.
            extra_laid, _, _ = layout(
                extra_px, src_w, src_h,
                size=params.page.size,
                orientation=params.page.orientation,
                margin_mm=params.page.margin_mm,
                reserve_bottom_mm=reserve_bottom_mm,
                padding_mm=params.page.padding_mm,
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
        # Layer-stats overlay table (citymap/airport path_counts, plotted in
        # a page corner). Empty rows are a no-op so the toggle can stay on
        # before the first import lands; bottom corners sit above the label
        # strip instead of sliding under it.
        table_lines: list = []
        if params.stats_table.enabled and params.stats_table.rows:
            table_lines, table_warnings, table_keepout = stats_table.layout_stats_table(
                [(r.key, r.value) for r in params.stats_table.rows],
                position=params.stats_table.position,
                page_w=page_w, page_h=page_h,
                margin_mm=params.page.margin_mm,
                reserve_bottom_mm=reserve_bottom_mm,
                pad_left_mm=params.stats_table.pad_left_mm,
                pad_right_mm=params.stats_table.pad_right_mm,
                pad_top_mm=params.stats_table.pad_top_mm,
                pad_bottom_mm=params.stats_table.pad_bottom_mm,
            )
            warnings.extend(table_warnings)
            if table_keepout is not None:
                # The table is a cartouche: the map must not show through
                # its text, borders or the inset strip around it.
                laid = knock_out(laid, table_keepout)
        t0 = time.perf_counter()
        # Merge domains separately: a joint linemerge would fuse image strokes
        # into divider/frame across the strip gap at high tolerances, dragging
        # artwork out of the image area (or the frame into it).
        q = settings.quantization_mm
        tol = params.linemerge_tolerance_mm
        _poll("vpype:linemerge")
        merged = linemerge(quantize(laid, q), tol)
        # Page furniture rejoins AFTER linesimplify below: simplify would
        # eat the 2 mm frame-radius arcs (0.04 mm chord sagitta < 0.1 mm
        # tolerance) and leave lathe chamfers, so it only ever sees artwork.
        # OCR text and exact circles keep their drawn shape like labels do.
        static_lines = lab_lines + frame_lines + table_lines + extra_laid
        static_merged: list = (
            linemerge(quantize(static_lines, q), tol) if static_lines else []
        )
        _timed("linemerge", image_id, method_label, t0)
        t0 = time.perf_counter()
        _poll("vpype:curvesmooth")
        smoothed = (
            curvesmooth(
                # Drawing mode: cap segment length first so Chaikin cannot
                # balloon long straight edges (legacy converts are unchanged).
                densify(merged, drawing.SMOOTH_SEG_MM) if drawing_mode else merged,
                params.curve_smooth)
            if params.curve_smooth > 0
            else merged
        )
        if smoothed is not merged:
            warnings.append("curve_smoothed")
        _timed("curvesmooth", image_id, method_label, t0)
        t0 = time.perf_counter()
        _poll("vpype:linesimplify")
        simplified = linesimplify(smoothed, params.linesimplify_tolerance_mm)
        # Page furniture kept its drawn shape (it bypassed linesimplify
        # above) — rejoin before travel optimization so pen travel stays
        # optimal across the whole sheet.
        simplified.extend(static_merged)
        _timed("linesimplify", image_id, method_label, t0)
        t0 = time.perf_counter()
        fast = is_fast_preview(params, is_vector)
        if fast and (params.linesort or params.reloop_tolerance_mm > 0):
            warnings.append("travel_optimization_off")
        _poll("vpype:linesort")
        ordered = linesort(simplified) if (params.linesort and not fast) else simplified
        _timed("linesort", image_id, method_label, t0)
        _poll("vpype:reloop")
        final = reloop(ordered, params.reloop_tolerance_mm) if not fast else ordered
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
        svg_text = to_svg(
            final, page_w, page_h,
            stroke_color=params.line_color,
            background_data_uri=get_background_data_uri(params.background),
        )
        # Ownership/licence notice (no-op unless configured or titled).
        svg_text = svgmeta.stamp(
            svg_text, settings, title=params.svg_meta.title,
            description=params.svg_meta.description)
        vpype_command = build_vpype_command(
            linemerge_tol=params.linemerge_tolerance_mm,
            linesimplify_tol=params.linesimplify_tolerance_mm,
            linesort_on=params.linesort and not fast,
            reloop_tol=params.reloop_tolerance_mm if not fast else None,
            page_size=params.page.size.upper(),
            margin_mm=params.page.margin_mm,
        )
        filename = convert_result_filename(image_id, params, is_vector, settings)
        return ConvertResult(
            svg_text=svg_text, filename=filename, stats=stats,
            warnings=warnings, vpype_command=vpype_command,
        )
    except PenPlotError:
        raise
    except Exception as exc:
        from backend.cancel import ClientCancelled as _CC2

        if isinstance(exc, _CC2):
            raise
        log.exception("pipeline.convert failed image=%s method=%s", image_id[:12], method_label)
        raise processing_failed() from exc
    finally:
        stack.close()  # never leave the heavy-trace slot held (idempotent)
