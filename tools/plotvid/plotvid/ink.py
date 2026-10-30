"""Ink check: how much new, persistent ink appears on the paper over a time span.

Why this is not a fixed-pixel ink map: the vid2 phone is hand-held. The view
pans, tilts and zooms, and the drawing slides around the frame. Ink is
compared after registering frames to each other, never at fixed pixels.

Per low-res grayscale frame (ffmpeg autorotates, so frames are portrait):
- **Ink candidates:** a black top-hat (closing minus image, 7 px) picks thin
  dark strokes: pen lines. Large dark regions do not respond, such as the
  arm, the pen body, the frame rails and shadows.
- **Occluder mask:** large dark blobs are found as a 9 px closing that is
  well below the local paper level (a 31 px dilation). They are dilated by
  11 px so the tapered pen tip next to the pen body is covered too.
- **Valid paper:** smoothed brightness above ``paper_level``, minus the
  occluder mask, intersected with the optional ``roi`` override.

Registration: consecutive frames are aligned with an affine ECC on the
blurred top-hat image, which is mostly ink and ignores the occluders. A
phase-correlation translation seeds it. Chained transforms map any frame
to any other. For each span, the start frame is re-aligned directly to the
end frame so chain drift does not matter.

New ink over a span [t0, t1] is the set of pixels that:
- are ink in at least ``persist_frac`` of the frames over ``persist_s``
  after t1 where they are visible, so a passing arm does not count; and
- were valid paper at t0 (eroded) but not ink there. The t0 ink is dilated
  by 2 px so a slight misregistration of old lines does not read as new.

The count is in pixels at ``width``. It is scaled to a 288 px reference so
the drawn threshold does not depend on the analysis width.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from plotvid.config import InkConfig

REF_WIDTH = 288

_K_TOPHAT = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
_K_BLOB = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
_K_GROW = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
_K_PAPER = cv2.getStructuringElement(cv2.MORPH_RECT, (31, 31))
_K_TOL = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
_ECC_CRITERIA = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 50, 1e-4)


def analysis_size(display_size: tuple[int, int], width: int) -> tuple[int, int]:
    """(w, h) of analysis frames: ``width`` wide, even height, display aspect kept."""
    dw, dh = display_size
    return width, max(2, round(width * dh / dw / 2) * 2)


def decode_gray(path: Path, fps: float, size: tuple[int, int], start_s: float = 0.0,
                duration_s: float | None = None) -> np.ndarray:
    """Low-res grayscale frames (N, H, W) uint8 at ``fps`` from ``start_s``.

    ffmpeg autorotates, so ``size`` is in display orientation."""
    w, h = size
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start_s > 0:
        cmd += ["-ss", f"{start_s:.3f}"]
    cmd += ["-i", str(path)]
    if duration_s is not None:
        cmd += ["-t", f"{duration_s:.3f}"]
    cmd += ["-vf", f"fps={fps},scale={w}:{h},format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    n = len(raw) // (w * h)
    return np.frombuffer(raw[: n * w * h], np.uint8).reshape(n, h, w)


@dataclass(frozen=True)
class FrameFeatures:
    ink: np.ndarray  # (N, H, W) uint8 0/1: thin dark strokes on valid paper
    valid: np.ndarray  # (N, H, W) uint8 0/1: paper visible and not occluded
    occluder: np.ndarray  # (N,) fraction of valid-or-occluded paper covered by the arm
    tophat: np.ndarray  # (N, H, W) float32, blurred, for registration


def roi_mask(shape: tuple[int, int], roi: tuple[float, float, float, float] | None) -> np.ndarray:
    h, w = shape
    m = np.zeros((h, w), np.uint8)
    if roi is None:
        m[:] = 1
        return m
    x0, y0, x1, y1 = roi
    m[int(y0 * h):int(np.ceil(y1 * h)), int(x0 * w):int(np.ceil(x1 * w))] = 1
    return m


def features(frames: np.ndarray, cfg: InkConfig) -> FrameFeatures:
    n, h, w = frames.shape
    ink = np.zeros((n, h, w), np.uint8)
    valid = np.zeros((n, h, w), np.uint8)
    top = np.zeros((n, h, w), np.float32)
    occ = np.zeros(n)
    roi = roi_mask((h, w), cfg.roi)
    for i, g in enumerate(frames):
        th = cv2.morphologyEx(g, cv2.MORPH_BLACKHAT, _K_TOPHAT)
        closed = cv2.morphologyEx(g, cv2.MORPH_CLOSE, _K_BLOB)
        paper = cv2.dilate(g, _K_PAPER).astype(np.float32)
        arm = cv2.dilate((closed < paper * cfg.occluder_ratio).astype(np.uint8), _K_GROW) > 0
        bright = cv2.GaussianBlur(closed, (0, 0), 5) > cfg.paper_level
        pm = bright & ~arm & (roi > 0)
        valid[i] = pm
        ink[i] = (th > cfg.ink_threshold) & pm
        top[i] = cv2.GaussianBlur(np.where(pm, th, 0).astype(np.float32), (0, 0), 1.5)
        area = (bright | arm) & (roi > 0)
        occ[i] = (arm & area).sum() / max(1, area.sum())
    return FrameFeatures(ink, valid, occ, top)


def _align(template: np.ndarray, image: np.ndarray, init: np.ndarray | None,
           window: np.ndarray) -> tuple[np.ndarray, bool]:
    """Affine ``W`` with template(x) ~ image(W x). Falls back to the seed on failure."""
    if init is None:
        (dx, dy), _ = cv2.phaseCorrelate(template, image, window)
        init = np.float32([[1, 0, dx], [0, 1, dy]])
    warp = init.astype(np.float32).copy()
    try:
        _, warp = cv2.findTransformECC(template, image, warp, cv2.MOTION_AFFINE,
                                       _ECC_CRITERIA, None, 5)
        return warp.astype(np.float32), True
    except cv2.error:
        return init.astype(np.float32), False


@dataclass
class InkAnalysis:
    fps: float
    width: int
    feats: FrameFeatures
    cum: np.ndarray  # (N, 3, 3): x_i = cum[i] @ x_0
    ecc_failures: int

    @property
    def n(self) -> int:
        return len(self.cum)

    def frame_at(self, t: float, side: str) -> int:
        f = t * self.fps
        i = int(np.floor(f)) if side == "before" else int(np.ceil(f))
        return min(self.n - 1, max(0, i))

    def _map(self, i: int, j: int) -> np.ndarray:
        return (self.cum[j] @ np.linalg.inv(self.cum[i]))[:2].astype(np.float32)

    def new_ink(self, t0: float, t1: float, cfg: InkConfig) -> float:
        """New persistent ink pixels (at REF_WIDTH scale) appearing over [t0, t1]."""
        f = self.feats
        _, h, w = f.ink.shape
        i0, i1 = self.frame_at(t0, "before"), self.frame_at(t1, "after")
        if i1 <= i0:
            return 0.0

        def warp(img, m):
            return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP,
                                  borderValue=0)

        cnt = np.zeros((h, w), np.int16)
        vis = np.zeros((h, w), np.int16)
        span = max(1, round(cfg.persist_s * self.fps))
        for j in range(i1, min(self.n, i1 + span + 1)):
            m = self._map(i1, j)
            cnt += warp(f.ink[j], m)
            vis += warp(f.valid[j], m)
        persist = (vis >= max(2, span // 2)) & (cnt >= cfg.persist_frac * vis)
        window = cv2.createHanningWindow((w, h), cv2.CV_32F)
        m, _ = _align(f.tophat[i1], f.tophat[i0], self._map(i1, i0), window)
        before = warp(cv2.dilate(f.ink[i0], _K_TOL), m) > 0
        bvalid = warp(cv2.erode(f.valid[i0], _K_TOL), m) > 0
        new = persist & bvalid & ~before
        return float(new.sum()) * REF_WIDTH / self.width


def analyse(path: Path, display_size: tuple[int, int], cfg: InkConfig) -> InkAnalysis:
    frames = decode_gray(path, cfg.fps, analysis_size(display_size, cfg.width))
    feats = features(frames, cfg)
    n, h, w = frames.shape
    window = cv2.createHanningWindow((w, h), cv2.CV_32F)
    cum = np.zeros((n, 3, 3))
    cum[0] = np.eye(3)
    fails = 0
    for i in range(1, n):
        m, ok = _align(feats.tophat[i - 1], feats.tophat[i], None, window)
        fails += not ok
        cum[i] = np.vstack([m, [0, 0, 1]]) @ cum[i - 1]
    return InkAnalysis(cfg.fps, w, feats, cum, fails)


def clean_frame_time(path: Path, display_size: tuple[int, int], duration_s: float,
                     cfg: InkConfig, search_s: float = 5.0) -> float:
    """Time in the last ``search_s`` where the arm covers the least paper.

    Cheap stand-in for "the final plot, arm out of the way". Ties go to the
    later frame (the drawing is most complete there)."""
    start = max(0.0, duration_s - search_s)
    frames = decode_gray(path, cfg.fps, analysis_size(display_size, cfg.width), start_s=start)
    if len(frames) == 0:
        return max(0.0, duration_s - 0.1)
    occ = features(frames, cfg).occluder
    # Last frame within 0.5 percentage points of the minimum; skip the very
    # last frame, which some encoders duplicate or truncate.
    usable = occ[:-1] if len(occ) > 1 else occ
    best = np.flatnonzero(usable <= usable.min() + 0.005)[-1]
    return start + best / cfg.fps
