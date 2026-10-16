"""Technical-drawing stage for the raster convert path.

Three opt-in pieces that turn a CAD-style scan (dimension drawings, blueprints)
into clean plotter geometry; all are off by default so existing converts are
byte-identical:

* ``ocr_text``  - read printed labels and re-draw them in the ZnikoSL
  single-stroke face (``analyse``);
* ``solids``    - trace the outline of solid-black areas (tyres, engines) instead of
                  skeletonising them into a squiggle (``solid_outlines``);
* ``circles``   - detect wheels/hubs/holes and draw exact circles (``analyse``);
* ``thin_lines`` / ``trace_upscale`` - keep pale 1 px strokes and give thinned
  curves sub-pixel accuracy (``build_ink``).

Sized for a 1-2 CPU instance (see ``Settings.ocr_*``): the OCR step has a
wall-clock budget, runs at most ``ocr_max_concurrent`` at a time per worker,
works on an image capped at ``ocr_max_dim_px`` and caches its words per
(image, tone settings) so slider tweaks never re-OCR.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import logging
import math
import os
import shutil
import threading
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from backend.penplot import arcs, imaging, ocr_text
from backend.penplot.config import Settings
from backend.penplot.methods import Polyline
from backend.penplot.schemas import OcrTextParams

log = logging.getLogger(__name__)

#: Level at which the Gaussian-smoothed ink mask is re-binarised. Lower keeps
#: thinner lines (a 1 px line peaks near 0.55) but fills the gaps in dense
#: parallel slats (0.3 turned loco louvres into blobs); 0.45 holds both.
MASK_SMOOTH_LEVEL = 0.45

#: Segment cap (mm) applied before Chaikin smoothing in drawing mode.
SMOOTH_SEG_MM = 1.5

#: Bump when the OCR engine changes what it reads, to retire cached words.
OCR_ENGINE_VERSION = "sheet-1"

WARNING_OCR_UNAVAILABLE = "ocr_unavailable"
WARNING_OCR_BUDGET = "ocr_time_budget_exceeded"
WARNING_OCR_BUSY = "ocr_busy"
WARNING_UPSCALE_CLAMPED = "trace_upscale_clamped"
WARNING_TRACE_BUSY = "trace_busy_low_quality"

#: One-click recipe for CAD-style dimension drawings (the /penplot "Technical
#: drawing" preset button and the ``img2plot`` CLI defaults). Plain JSON-able
#: ``ConvertParams`` fields, so the page, the CLI and the API cannot drift.
DRAWING_PRESET: dict = {
    "methods": ["centerline"],
    "threshold": 215,
    "blur_radius": 0.7,
    "thin_lines": True,
    "trace_upscale": 3,
    "contour_simplify": 0.7,
    "centerline_prune_px": 3,
    "curve_smooth": 3,
    "circles": True,
    "strip_frame": True,
    "ocr_text": {"enabled": True, "min_conf": 30.0, "min_chars": 3},
    "page": {"size": "A4", "orientation": "landscape", "margin_mm": 10.0,
             "padding_mm": 0.0, "frame": False},
}

_cache_lock = threading.Lock()


@dataclass
class Analysis:
    """Result of :func:`analyse`: the text/circle-masked raster plus the
    replacement strokes, all in the (working) image's pixel space."""

    cleaned: np.ndarray
    text_px: list[Polyline] = field(default_factory=list)
    circle_px: list[Polyline] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def is_drawing_mode(params) -> bool:
    """Any drawing-stage option on (the pipeline then takes the tone-first path)."""
    return bool(params.thin_lines or params.circles or params.solids
                or params.ocr_text.enabled or params.trace_upscale > 1)


def _tesseract_available() -> bool:
    """The Python binding and the ``tesseract`` binary are both present."""
    return (importlib.util.find_spec("pytesseract") is not None
            and shutil.which("tesseract") is not None)


def _tesserocr_available() -> bool:
    """The ``tesserocr`` wheel (bundled engine) plus the vendored model."""
    return ocr_text._tesserocr is not None and ocr_text.tessdata_present()


#: name -> (available(), reader factory). Order matters for ``auto``: the
#: in-process wheel first (no system package needed), then the system binary.
#: To try another recogniser, register it here (or monkeypatch in tests) and
#: select it with ``PENPLOT_OCR_BACKEND``; the contract is ``ocr_text.SheetReader``.
OCR_BACKENDS: dict[str, tuple] = {
    "tesserocr": (_tesserocr_available, lambda: ocr_text.tesserocr_reader),
    "tesseract": (_tesseract_available, lambda: ocr_text.tesseract_reader),
}


def resolve_backend(settings: Settings | None = None) -> str | None:
    """Backend name to use (``auto`` = first available), or None."""
    name = (settings or Settings()).ocr_backend
    if name == "auto":
        return next((n for n, (ok, _f) in OCR_BACKENDS.items() if ok()), None)
    entry = OCR_BACKENDS.get(name)
    return name if entry and entry[0]() else None


def ocr_available(settings: Settings | None = None) -> bool:
    """Is a text-recognition backend usable on this host?"""
    return resolve_backend(settings) is not None


def _acquire_slot(settings: Settings, cancelled, give_up: float, *,
                  prefix: str = "slot", n: int | None = None) -> int | None:
    """Take one of ``n`` machine-wide slots (default ``ocr_max_concurrent``).

    Slots are ``flock`` files under ``ocr_dir``, so the limit holds across all
    uvicorn workers (and threads) on the box, not per process: a 1-2 CPU
    instance running 4 workers still does at most N OCR runs at once. Polls
    every 200 ms; raises ``ClientCancelled`` if the request is cancelled while
    queued; returns None once ``give_up`` (monotonic) passes. The kernel drops
    a lock when its process dies, so a crashed worker cannot wedge a slot.
    """
    from backend.cancel import ClientCancelled  # noqa: PLC0415

    os.makedirs(settings.ocr_dir, exist_ok=True)
    while True:
        for name in _slot_names(prefix, settings.ocr_max_concurrent if n is None else n):
            fd = os.open(os.path.join(settings.ocr_dir, name),
                         os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except OSError:
                os.close(fd)
        if cancelled is not None and cancelled():
            raise ClientCancelled("/v1/convert", "ocr")
        if time.monotonic() > give_up:
            return None
        time.sleep(0.2)


def _slot_names(prefix: str, n: int) -> list[str]:
    return [f".{prefix}{i}" for i in range(max(1, n))]


def _release_slot(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --------------------------------------------------------------------- cache

def _cache_path(settings: Settings, image_id: str, tone_key: str) -> str:
    return os.path.join(settings.ocr_dir, f"{image_id}_{tone_key}.json")


def _tone_key(settings: Settings, tone_args: tuple) -> str:
    raw = json.dumps([OCR_ENGINE_VERSION, settings.ocr_max_dim_px, *tone_args])
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _load_words(path: str, ttl_hours: int) -> list[ocr_text.Word] | None:
    if not ttl_hours:
        return None
    with _cache_lock:
        try:
            if time.time() - os.path.getmtime(path) > ttl_hours * 3600.0:
                os.remove(path)
                return None
            with open(path, "r", encoding="utf-8") as fh:
                rows = json.load(fh)
            return [ocr_text.Word(**row) for row in rows]
        except (OSError, ValueError, TypeError):
            return None


def _store_words(path: str, words: list[ocr_text.Word]) -> None:
    with _cache_lock:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump([w.__dict__ for w in words], fh)
            os.replace(tmp, path)
        except OSError:
            log.warning("drawing: could not write OCR cache %s", path)


@contextlib.contextmanager
def trace_slot(settings: Settings, cancelled=None):
    """Machine-wide slot for the memory-heavy part of a drawing-mode convert.

    Yields True when held; False when none freed up within ``ocr_queue_s`` (the
    caller then drops to 1x instead of risking an out-of-memory kill next to
    another heavy trace). Released on exit; the kernel also drops it if the
    process dies.
    """
    fd = _acquire_slot(settings, cancelled, time.monotonic() + settings.ocr_queue_s,
                       prefix="trace", n=settings.trace_max_concurrent)
    try:
        yield fd is not None
    finally:
        if fd is not None:
            _release_slot(fd)


# ----------------------------------------------------------------------- OCR

def _read_words(
    small: np.ndarray, *, settings: Settings, cancelled, warnings: list[str],
) -> list[ocr_text.Word] | None:
    """Run the contact-sheet OCR under a machine-wide slot and time budget.

    Returns None when OCR was skipped or cut short (partial results are not
    trusted/cached); the caller then keeps the text as traced lines.
    """
    fd = _acquire_slot(settings, cancelled, time.monotonic() + settings.ocr_queue_s)
    if fd is None:
        warnings.append(WARNING_OCR_BUSY)
        return None
    try:
        engine = ocr_text.SheetEngine(
            reader=OCR_BACKENDS[resolve_backend(settings)][1](),
            workers=max(1, min(settings.ocr_workers, os.cpu_count() or 1)),
            deadline=time.monotonic() + settings.ocr_budget_s,
            cancelled=cancelled)
        words = engine(small)
        if engine.budget_exceeded:
            warnings.append(WARNING_OCR_BUDGET)
            return None
        return words
    finally:
        _release_slot(fd)


def _scaled(words: list[ocr_text.Word], k: float) -> list[ocr_text.Word]:
    return [ocr_text.Word(w.text, w.x * k, w.y * k, w.w * k, w.h * k, w.conf, w.angle)
            for w in words]


#: Colour emblems (flags, logos): saturated, bright, compact. A picture that is
#: more than COLOR_MAX_FRACTION saturated is coloured artwork: leave it alone.
COLOR_MIN_SATURATION = 90
COLOR_MIN_VALUE = 60
COLOR_MAX_FRACTION = 0.08
COLOR_MIN_AREA_FRACTION = 0.0003
COLOR_MAX_AREA_FRACTION = 0.05
#: Share of a blob's bounding box that is genuinely saturated. A flag is a solid
#: patch (~0.5); JPEG colour fringes around black glyphs are sparse (< 0.1).
COLOR_MIN_FILL = 0.25


def erase_color_blobs(gray: np.ndarray, image_bytes: bytes) -> tuple[np.ndarray, int]:
    """Whiten saturated-colour blobs (flag, logo) of the source in ``gray``.

    Grayscale loading discards colour, which is what tells a flag from line
    art, so the original bytes are decoded again here. Returns ``(gray, n)``
    with ``n`` blobs wiped; best effort (undecodable input: unchanged).
    """
    bgr = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        return gray, 0
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    raw = ((hsv[..., 1] >= COLOR_MIN_SATURATION)
           & (hsv[..., 2] >= COLOR_MIN_VALUE)).astype(np.uint8)
    if not raw.any() or raw.mean() > COLOR_MAX_FRACTION:
        return gray, 0
    sat = cv2.dilate(raw, np.ones((7, 7), np.uint8))   # fuse a flag's stripes
    H, W = sat.shape
    n, lab, st, _ = cv2.connectedComponentsWithStats(sat, connectivity=8)
    out, wiped = gray.copy(), 0
    gh, gw = gray.shape[:2]
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in st[i])
        if not (COLOR_MIN_AREA_FRACTION * H * W <= area <= COLOR_MAX_AREA_FRACTION * H * W):
            continue
        patch = lab[y:y + h, x:x + w] == i
        if raw[y:y + h, x:x + w][patch].sum() < COLOR_MIN_FILL * w * h:
            continue  # sparse colour noise around glyphs, not a solid emblem
        x0, x1 = int(x * gw / W) - 3, int((x + w) * gw / W) + 4
        y0, y1 = int(y * gh / H) - 3, int((y + h) * gh / H) + 4
        out[max(y0, 0):y1, max(x0, 0):x1] = 255
        wiped += 1
    return out, wiped


def _erase_titles(tone: np.ndarray, img: np.ndarray, settings: Settings, small_of,
                  shrink: float) -> np.ndarray:
    """Wipe confidently-read, title-sized words (letters allowed) from ``img``.

    The words are found on ``tone`` (the untouched page): ``img`` has already
    lost its digit labels, which would leave a half-read title behind.

    The label engine reads digits only, so a caption such as "Aero A.100" is
    invisible to it; one whole-page read finds it. Best effort: any OCR
    failure leaves the image untouched.
    """
    try:
        reader = OCR_BACKENDS[resolve_backend(settings)][1]()
        page = ocr_text._PAGE_FOR[reader]
        boxes = ocr_text.erase_boxes(page(small_of(tone)))
    except Exception:  # noqa: BLE001 - optional step, never fatal
        log.warning("erase_text: whole-page read failed", exc_info=True)
        return img
    if not boxes:
        return img
    k = 1.0 / shrink
    return ocr_text.wipe(img, [(int(a * k), int(b * k), int(c * k) + 1, int(e * k) + 1)
                               for a, b, c, e in boxes])


def analyse(
    tone: np.ndarray, *, ocr: OcrTextParams, circles: bool, settings: Settings,
    image_id: str, tone_args: tuple, cancelled=None,
) -> Analysis:
    """OCR + circle detection on the tone-adjusted (un-blurred) image.

    ``tone_args`` (contrast, brightness, remove_background, image size ...) keys
    the OCR cache: the words, and the pixel space their boxes live in, depend
    on them and nothing else.
    """
    warnings: list[str] = []
    cleaned = tone
    text_px: list[Polyline] = []
    h, w = tone.shape[:2]
    shrink = min(1.0, settings.ocr_max_dim_px / float(max(h, w)))

    def small_of(img: np.ndarray) -> np.ndarray:
        if shrink >= 1.0:
            return img
        return cv2.resize(img, None, fx=shrink, fy=shrink, interpolation=cv2.INTER_AREA)

    if ocr.enabled:
        if not ocr_available(settings):
            warnings.append(WARNING_OCR_UNAVAILABLE)
        else:
            path = _cache_path(settings, image_id, _tone_key(settings, tone_args))
            words = _load_words(path, settings.ocr_ttl_hours)
            if words is None:
                read = _read_words(small_of(tone), settings=settings,
                                   cancelled=cancelled, warnings=warnings)
                if read is not None:
                    words = _scaled(read, 1.0 / shrink)
                    _store_words(path, words)
            if words is not None:
                cleaned, text_px, ocr_warnings = ocr_text.prepare(
                    tone, lambda _g, ws=words: ws, min_conf=ocr.min_conf,
                    min_chars=ocr.min_chars)
                if ocr.erase:  # recognised text is wiped, not re-drawn
                    text_px = []
                    cleaned = _erase_titles(tone, cleaned, settings, small_of, shrink)
                warnings.extend(ocr_warnings)

    circle_px: list[Polyline] = []
    if circles:
        rings = arcs.detect_circles(small_of(cleaned))
        if shrink < 1.0:
            rings = [(cx / shrink, cy / shrink, r / shrink) for cx, cy, r in rings]
        if rings:
            cleaned = arcs.mask_circles(cleaned, rings)
            circle_px = [arcs.circle_polyline(*c) for c in rings]
    return Analysis(cleaned, text_px, circle_px, warnings)


# ---------------------------------------------------------------- page frame

WARNING_COLOR_ERASED = "color_emblem_erased"
WARNING_FRAME_REMOVED = "page_frame_removed"
WARNING_FRAME_NOT_FOUND = "page_frame_not_found"


def detect_frame(
    gray: np.ndarray, *, band: float = 0.12, cover: float = 0.8, min_sides: int = 3,
    paper: int = 235, inset_px: int = 2, max_gap_px: int = 12,
) -> tuple[int, int, int, int] | None:
    """Find a border drawn around the artwork; return the crop box *inside* it.

    CAD sheets often carry their own frame (sometimes double). The project draws
    its own frame/label/radius, so the source one should go. A frame side is a
    straight line that spans >= ``cover`` of the image and sits in the outer
    ``band`` of that edge; parallel lines within ``max_gap_px`` of each other
    (double borders) are one frame. At least ``min_sides`` of the four sides must
    be found (a lone ground line therefore never qualifies); a missing side
    keeps the image edge. Returns ``(x0, y0, x1, y1)`` for ``gray[y0:y1, x0:x1]``
    or None. Pure NumPy/OpenCV, O(pixels).
    """
    h, w = gray.shape[:2]
    ink = (gray < paper).astype(np.uint8)
    close = np.ones((1, 7), np.uint8)
    hl = cv2.morphologyEx(cv2.morphologyEx(ink, cv2.MORPH_CLOSE, close), cv2.MORPH_OPEN,
                          np.ones((1, max(int(cover * w), 3)), np.uint8))
    vl = cv2.morphologyEx(cv2.morphologyEx(ink, cv2.MORPH_CLOSE, close.T), cv2.MORPH_OPEN,
                          np.ones((max(int(cover * h), 3), 1), np.uint8))
    rows = np.flatnonzero(hl.any(axis=1))
    cols = np.flatnonzero(vl.any(axis=0))

    def inner_edge(idx: np.ndarray, lo: int, hi: int, from_low: bool) -> int | None:
        """Inner edge of the outermost line cluster inside [lo, hi)."""
        cand = idx[(idx >= lo) & (idx < hi)]
        if cand.size == 0 or cand.size > (hi - lo) * 0.5:  # solid fill, not a line
            return None
        cand = cand if from_low else cand[::-1]
        edge = int(cand[0])
        for c in cand[1:]:
            if abs(int(c) - edge) <= max_gap_px:
                edge = int(c)
            else:
                break
        return edge

    bh, bw = int(band * h), int(band * w)
    top = inner_edge(rows, 0, bh, True)
    bottom = inner_edge(rows, h - bh, h, False)
    left = inner_edge(cols, 0, bw, True)
    right = inner_edge(cols, w - bw, w, False)
    if sum(v is not None for v in (top, bottom, left, right)) < min_sides:
        return None
    x0 = 0 if left is None else left + 1 + inset_px
    y0 = 0 if top is None else top + 1 + inset_px
    x1 = w if right is None else right - inset_px
    y1 = h if bottom is None else bottom - inset_px
    if x1 - x0 < w * 0.5 or y1 - y0 < h * 0.5:  # implausible: not a frame
        return None
    return x0, y0, x1, y1


def strip_frame(gray: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """Crop ``gray`` to the inside of its page frame, if it has one."""
    box = detect_frame(gray)
    if box is None:
        return gray, [WARNING_FRAME_NOT_FOUND]
    x0, y0, x1, y1 = box
    return np.ascontiguousarray(gray[y0:y1, x0:x1]), [WARNING_FRAME_REMOVED]


# ------------------------------------------------------------------ the mask

def mask_level(blur: float) -> float:
    """Re-binarise level for a Gaussian-smoothed mask of strength ``blur``.

    A 1 px line peaks at ~0.40/blur after smoothing (independent of the
    supersampling factor, since the kernel scales with it). The level follows
    that, capped at ``MASK_SMOOTH_LEVEL``, so ``thin_lines`` keeps 1 px strokes
    at any blur setting; at the preset's 0.7 it is exactly the cap.
    """
    return min(MASK_SMOOTH_LEVEL, 0.8 * 0.3989 / max(blur, 1e-6))


#: A pixel belongs to a solid area when it is this far (1x px) from the nearest
#: paper pixel: ordinary strokes are 1-3 px wide (half-width < 2), tyres and
#: engine blocks are 6+ px across.
SOLID_HALF_WIDTH_PX = 5.0
#: Solid areas smaller than this (1x px^2) are specks, not parts.
SOLID_MIN_AREA_PX = 120.0


def solid_outlines(ink: np.ndarray, u: int) -> tuple[np.ndarray, list[Polyline]]:
    """Split solid-black areas off the ink mask and return their outlines.

    Centreline tracing turns a filled tyre into one squiggly stroke. Here
    every area thicker than ``SOLID_HALF_WIDTH_PX`` (opened back to its true
    extent) is removed from ``ink`` and its outer contour is returned instead,
    as closed polylines in the mask's (``u``-scaled) pixels. Thin strokes
    attached to a solid are left to the skeleton, which then ends at its rim.
    """
    ink_u8 = ink.astype(np.uint8)
    dist = cv2.distanceTransform(ink_u8, cv2.DIST_L2, 3)
    r = SOLID_HALF_WIDTH_PX * u
    core = (dist >= r).astype(np.uint8)
    if not core.any():
        return ink, []
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * int(r) + 1, 2 * int(r) + 1))
    solid = cv2.dilate(core, k) & ink_u8      # the cores grown back to their rim
    n, lab, st, _ = cv2.connectedComponentsWithStats(solid, connectivity=8)
    keep = np.zeros_like(solid)
    outlines: list[Polyline] = []
    for i in range(1, n):
        if st[i][4] < SOLID_MIN_AREA_PX * u * u:
            continue
        comp = (lab == i).astype(np.uint8)
        keep |= comp
        cnts, _ = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        for c in cnts:
            c = cv2.approxPolyDP(c, 0.7 * u, True)
            if len(c) >= 3:
                pts = [(float(x), float(y)) for x, y in c[:, 0, :]]
                outlines.append(pts + [pts[0]])
    if not outlines:
        return ink, []
    # Remove the solids (plus a 1 px rim, so no sliver of them is skeletonised).
    cleaned = ink_u8 & ~cv2.dilate(keep, np.ones((3, 3), np.uint8))
    return cleaned.astype(bool), outlines



def build_ink(
    gray: np.ndarray, *, threshold: int, blur: float, thin_lines: bool,
    upscale: int, max_pixels: int,
) -> tuple[np.ndarray, np.ndarray, int, list[str]]:
    """Ink mask for tracing: ``(gray_u, ink, u, warnings)`` at ``u``-fold scale.

    ``u`` is the requested upscale clamped so the supersampled image stays
    within ``max_pixels`` in total. With ``thin_lines`` the greyscale is binarised first and the
    *mask* is smoothed; otherwise the greyscale is blurred and thresholded
    (the legacy order). Polylines traced on the result are in ``u``-scaled
    pixels; the caller divides by ``u``.
    """
    warnings: list[str] = []
    area = max(gray.shape[0] * gray.shape[1], 1)
    u = max(1, min(int(upscale), int(math.sqrt(max_pixels / area))))
    if u < upscale:
        warnings.append(WARNING_UPSCALE_CLAMPED)
    gray_u = gray
    if u > 1:
        gray_u = cv2.resize(gray, None, fx=u, fy=u, interpolation=cv2.INTER_CUBIC)
    if thin_lines:
        ink = imaging.threshold_mask(gray_u, threshold)
        if blur > 0:
            # 8-bit (not float32) smoothing: a quarter of the memory per pixel.
            soft = cv2.GaussianBlur(ink.view(np.uint8) * np.uint8(255), (0, 0), blur * u)
            ink = soft >= int(round(mask_level(blur) * 255))
            del soft
    else:
        gray_u = imaging.blur(gray_u, blur * u)
        ink = imaging.threshold_mask(gray_u, threshold)
    return gray_u, ink, u, warnings
