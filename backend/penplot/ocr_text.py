"""OCR text -> single-stroke text stage (spike).

Flow: ``detect_words`` (pluggable engine) -> ``mask_words`` (blank the glyph
pixels so the line tracers don't vectorise letters as scribbles) -> trace the
cleaned raster with the usual methods -> ``words_to_polylines`` (re-set each
word in the ZnikoSL single-stroke face from ``svgfont``, fitted to its source box).

Words below ``min_conf`` are neither masked nor replaced: they stay as traced
lines, so a misread never overwrites correct artwork.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

import cv2
import numpy as np

from backend.penplot import svgfont
from backend.penplot.methods import Polyline

log = logging.getLogger(__name__)

# tesserocr (bundled libtesseract) pulls in cysignals, which installs signal
# handlers at import and therefore must be imported on the MAIN thread; the
# convert runs on worker threads. So import it here, once, at app start (this
# module is imported by backend.main). OMP_THREAD_LIMIT must be set before
# libtesseract loads. Absent on hosts without the wheel -> None.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")
try:
    import tesserocr as _tesserocr
except Exception:  # not installed / wheel unavailable on this platform
    _tesserocr = None

WARNING_UNSUPPORTED = "ocr_unsupported_characters"
WARNING_LOW_CONF = "ocr_low_confidence_words_kept_as_lines"


def layout_text(text: str) -> tuple[list[Polyline], list[str], float]:
    """Lay ``text`` on one line in ZnikoSL face units (shared ``svgfont`` face).

    Face space is y-down from the cap top: caps span y 0..cap_height, descenders
    go below. Returns ``(polylines, skipped_chars, width)``; space advances by
    ``space_advance`` (it has no glyph), unsupported characters are skipped.
    """
    face = svgfont.get_svg_face()
    glyphs = face["glyphs"]
    out: list[Polyline] = []
    skipped: list[str] = []
    x = 0.0
    for ch in text:
        if ch == " ":
            x += face["space_advance"]
            continue
        g = glyphs.get(ch)
        if g is None:
            skipped.append(ch)
            continue
        out.extend([(px + x, py) for px, py in line] for line in g["lines"])
        x += g["advance"]
    return out, skipped, x


@dataclass(frozen=True)
class Word:
    text: str
    #: Axis-aligned box in source-image px (x, y, w, h) of the *unrotated* word.
    x: float
    y: float
    w: float
    h: float
    conf: float  # 0..100
    #: Clockwise text rotation in degrees (0 = horizontal, 90 = reads downward).
    angle: float = 0.0


Engine = Callable[[np.ndarray], list[Word]]


def tesseract_engine(psm: int = 11, lang: str = "eng") -> Engine:
    """Engine backed by pytesseract (optional dependency, imported lazily)."""
    import pytesseract  # noqa: PLC0415

    def run(gray: np.ndarray) -> list[Word]:
        data = pytesseract.image_to_data(
            gray, lang=lang, config=f"--psm {psm}",
            output_type=pytesseract.Output.DICT)
        words: list[Word] = []
        for i, txt in enumerate(data["text"]):
            txt = txt.strip()
            if not txt:
                continue
            words.append(Word(
                text=txt, x=float(data["left"][i]), y=float(data["top"][i]),
                w=float(data["width"][i]), h=float(data["height"][i]),
                conf=float(data["conf"][i])))
        return words

    return run


#: Ink cutoffs for blob detection, strict first: dark labels stay separate from
#: neighbours at 140, pale italic ones ("7°") only hold together near 185.
INK_THRESHOLDS = (140, 185, 215)


def strip_lines(gray: np.ndarray, min_len: int = 25, grow: int = 3) -> np.ndarray:
    """Paint long horizontal/vertical runs white so labels touching dimension
    or extension lines separate from them (a leading '3' fused to an arrow
    line otherwise forms one oversized blob and is dropped)."""
    ink = (gray < 140).astype(np.uint8)
    hl = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                          cv2.getStructuringElement(cv2.MORPH_RECT, (min_len, 1)))
    vl = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                          cv2.getStructuringElement(cv2.MORPH_RECT, (1, min_len)))
    # ``grow`` widens the removed line to take its anti-aliased halo, but a
    # number sitting right on its dimension line loses the glyph row touching
    # it (7000, 1510, 21000 lost their bottoms); ``grow=0`` keeps those rows.
    lines = hl | vl
    if grow:
        lines = cv2.dilate(lines, np.ones((grow, grow), np.uint8))
    out = gray.copy()
    out[lines > 0] = 255
    return out


def _text_candidates(gray: np.ndarray, ink_threshold: int = 140, *,
                     stripped: bool = False) -> list[tuple[int, int, int, int, str]]:
    """Glyph-sized ink blobs merged into word boxes: (x, y, w, h, "h"|"v").

    Whole-page OCR on a sparse technical drawing misses most dimension
    numerals; finding glyph-sized components first and OCRing each box alone
    is far more reliable. Size limits are tuned for ~7-15 px glyphs.
    """
    ink = ((gray if stripped else strip_lines(gray)) < ink_threshold).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    big = np.zeros(n, bool)
    small = np.zeros(n, bool)
    for i in range(1, n):
        _, _, w, h, _ = st[i]
        big[i] = 5 <= max(w, h) <= 15 and 2 <= min(w, h) <= 13
        # degree rings / dots: only count next to a normal glyph (see below)
        small[i] = 3 <= max(w, h) <= 5 and 2 <= min(w, h)
    big_px = big[lab].astype(np.uint8)
    glyphs = (big | small)[lab].astype(np.uint8)
    out = []
    for orient, k in (("h", (7, 3)), ("v", (3, 7))):
        merged = cv2.dilate(glyphs, cv2.getStructuringElement(cv2.MORPH_RECT, k))
        m, mlab, s2, _ = cv2.connectedComponentsWithStats(merged)
        for i in range(1, m):
            x, y, w, h, _ = (int(v) for v in s2[i])
            long_, short = (w, h) if orient == "h" else (h, w)
            if not (long_ >= 10 and 6 <= short <= 22):
                continue
            region = mlab[y:y + h, x:x + w] == i
            if not big_px[y:y + h, x:x + w][region].any():
                continue  # specks only
            n_glyphs = cv2.connectedComponents(glyphs[y:y + h, x:x + w] * region)[0] - 1
            if n_glyphs >= 2:
                out.append((x, y, w, h, orient))
    return out


def _overlap(a, b) -> float:
    """Intersection area as a fraction of the smaller box."""
    ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return ix * iy / max(min(a[2] * a[3], b[2] * b[3]), 1)


def _replaces(new: Word, clash: list[Word]) -> bool:
    """Should ``new`` take over the boxes it overlaps (``clash``)?

    More confident wins, except that a reading which is just a cut-off version
    of the other ("197" from a clipped "1975") always loses to the longer one.
    """
    for m in clash:
        a, b = new.text, m.text
        if a != b and b in a and new.conf >= 25:
            continue  # new extends m: acceptable even when less confident
        if a != b and a in b:
            return False  # new is a fragment of m
        if new.conf <= m.conf:
            return False
    return True


def find_repeats(gray: np.ndarray, words: list[Word], *, min_score: float = 0.8) -> list[Word]:
    """Copies of already-read horizontal words elsewhere on the sheet.

    Drawings repeat labels (the two "7" slope marks, matching dimension
    values). A copy can fail OCR on its own, e.g. an arrowhead fused to it
    breaks blob detection, yet is a near pixel-identical match for a word that
    did read. Template-match each read word over the image and add the
    non-overlapping hits with the same text.
    """
    found: list[Word] = []
    taken = [(w.x, w.y, w.w, w.h) for w in words]
    for w in words:
        if w.angle != 0.0 or w.w < 6 or w.h < 6:
            continue
        x0, y0 = int(w.x), int(w.y)
        tpl = gray[y0:int(w.y + w.h) + 1, x0:int(w.x + w.w) + 1]
        if tpl.shape[0] < 6 or tpl.shape[1] < 6 or tpl.std() < 1:
            continue
        score = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(score >= min_score)
        for sc, x, y in sorted(((float(score[y, x]), int(x), int(y))
                                for y, x in zip(ys, xs)), reverse=True):
            box = (float(x), float(y), w.w, w.h)
            if any(_overlap(t, box) > 0.3 for t in taken):
                continue
            taken.append(box)
            found.append(Word(w.text, *box, conf=w.conf * sc, angle=0.0))
    return found


#: Contact-sheet variants (scale, shear, binarisation). Round 1 runs on every
#: candidate; round 2 only on cells whose round-1 reading was not decisive.
_ROUNDS: tuple[tuple[tuple[int, float, str], ...], ...] = (
    # round 1: upright only - sheared variants of upright text are noise votes
    tuple((sc, 0.0, post) for post in ("soft3", "soft6", "blurotsu") for sc in (3, 4, 5, 6, 8)),
    tuple((sc, sh, post) for sc in (4, 6, 8) for sh in (0.0, 0.1, 0.2)
          for post in ("gray", "blurotsu")),
)
#: A cell is settled once its leading reading has this many votes and share.
_DECIDE_VOTES = 6
_DECIDE_SHARE = 0.75
#: Votes a reading needs before its share is measured among non-empty reads.
_MIN_AGREE = 3
#: Added to a degree label's vote share (digit + small ring).
RING_BONUS = 0.3
#: Readings below this vote share are reported but do not claim their box.
WEAK_SHARE = 0.2
#: Cells per contact sheet (one tall column; Tesseract page-segmentation 6).
_SHEET_CELLS = 60
_SHEET_PAD = 40


def _has_ring(crop: np.ndarray) -> bool:
    """True for the exact shape of a degree label: one digit-sized component and
    one small ring/dot up-right of it, and nothing else in the crop. Anything
    busier is clutter, not "7°"."""
    ink = (crop < INK_THRESHOLDS[1]).astype(np.uint8)
    n, _, st, cen = cv2.connectedComponentsWithStats(ink, connectivity=8)
    if n != 3:  # background + exactly two components
        return False
    digit, ring = sorted(range(1, 3), key=lambda i: -max(st[i][2], st[i][3]))
    return (max(st[digit][2], st[digit][3]) >= 6
            and 3 <= max(st[ring][2], st[ring][3]) <= 5
            and cen[ring][0] > cen[digit][0] and cen[ring][1] < cen[digit][1])


def _sheet(cells: list[np.ndarray], scale: int, shear: float, post: str):
    """Stack upscaled crops in one column; return ``(sheet, row_bounds)``."""
    rows = []
    for crop in cells:
        soft = post in ("soft3", "soft6")
        r = cv2.resize(crop, None, fx=scale, fy=scale,
                       interpolation=cv2.INTER_LANCZOS4 if soft else cv2.INTER_CUBIC)
        if shear:  # de-slant italic dimension text (top leans right)
            hh, ww = r.shape
            m = np.float32([[1, shear, -shear * hh / 2], [0, 1, 0]])
            r = cv2.warpAffine(r, m, (ww + int(shear * hh), hh), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=255)
        if soft:  # light blur, no binarisation: smooths thin serifs, keeps tone
            r = cv2.GaussianBlur(r, (0, 0), scale * (0.3 if post == "soft3" else 0.6))
        elif post == "blurotsu":
            r = cv2.GaussianBlur(r, (0, 0), scale * 0.5)
            _, r = cv2.threshold(r, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        rows.append(r)
    pad = _SHEET_PAD
    width = max(r.shape[1] for r in rows) + 2 * pad
    height = sum(r.shape[0] + 2 * pad for r in rows)
    sheet = np.full((height, width), 255, np.uint8)
    bounds, y = [], 0
    for r in rows:
        sheet[y + pad:y + pad + r.shape[0], pad:pad + r.shape[1]] = r
        bounds.append((y, y + r.shape[0] + 2 * pad))
        y += r.shape[0] + 2 * pad
    return sheet, bounds


#: A sheet reader turns one contact sheet (rows = candidate crops, white
#: padding between) into the text of each row. ``bounds`` are the ``(y0, y1)``
#: pixel extents of the rows. This is the seam for alternatives to Tesseract:
#: any callable with this signature works (``SheetEngine(reader=...)``); a
#: per-crop classifier can simply slice ``sheet[y0:y1]`` per row.
SheetReader = Callable[[np.ndarray, "list[tuple[int, int]]", str], "list[str]"]


def tesseract_reader(sheet: np.ndarray, bounds, whitelist: str) -> list[str]:
    """One Tesseract call for a whole sheet; text per cell (row)."""
    import pytesseract  # noqa: PLC0415

    d = pytesseract.image_to_data(
        sheet, config=f"--psm 6 -c tessedit_char_whitelist={whitelist}",
        output_type=pytesseract.Output.DICT)
    per: list[list[tuple[int, str]]] = [[] for _ in bounds]
    for t, left, top, h in zip(d["text"], d["left"], d["top"], d["height"]):
        if not t.strip():
            continue
        cy = top + h / 2
        for k, (y0, y1) in enumerate(bounds):
            if y0 <= cy < y1:
                per[k].append((left, t))
                break
    return ["".join(t for _, t in sorted(p)) for p in per]


TESSDATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tessdata")
_tls = threading.local()


def tessdata_present() -> bool:
    """The vendored English model (``eng.traineddata``, tessdata_fast, Apache-2.0)."""
    return os.path.exists(os.path.join(TESSDATA_DIR, "eng.traineddata"))


def tesserocr_reader(sheet: np.ndarray, bounds, whitelist: str) -> list[str]:
    """In-process Tesseract via the ``tesserocr`` wheel (bundles libtesseract).

    Needs no system package and no subprocess, so it works on hosts that only
    install Python dependencies (FastAPI Cloud); the model is vendored under
    ``penplot/tessdata``. One API object per thread (they are not thread-safe).
    """
    tesserocr = _tesserocr
    if tesserocr is None:
        raise RuntimeError("tesserocr is not installed")
    from PIL import Image  # noqa: PLC0415

    api = getattr(_tls, "api", None)
    if api is None or getattr(_tls, "whitelist", None) != whitelist:
        if api is not None:
            api.End()
        api = tesserocr.PyTessBaseAPI(
            path=TESSDATA_DIR, lang="eng", psm=tesserocr.PSM.SINGLE_BLOCK,
            oem=tesserocr.OEM.LSTM_ONLY)
        api.SetVariable("tessedit_char_whitelist", whitelist)
        _tls.api, _tls.whitelist = api, whitelist
    api.SetImage(Image.fromarray(sheet))
    api.Recognize()
    per: list[list[tuple[int, str]]] = [[] for _ in bounds]
    level = tesserocr.RIL.WORD
    it = api.GetIterator()
    if it is not None:
        for word in tesserocr.iterate_level(it, level):
            try:  # tesserocr raises (not "") for a word with no text
                text = (word.GetUTF8Text(level) or "").strip()
                box = word.BoundingBox(level)
            except RuntimeError:
                continue
            if not text or box is None:
                continue
            left, top, _right, bottom = box
            cy = (top + bottom) / 2
            for k, (y0, y1) in enumerate(bounds):
                if y0 <= cy < y1:
                    per[k].append((left, text))
                    break
    return ["".join(t for _, t in sorted(p)) for p in per]


#: A line reader turns one padded single-line crop into its text ("" if none).
LineReader = Callable[[np.ndarray, str], str]


def tesseract_line(img: np.ndarray, whitelist: str) -> str:
    """Single-line read through the ``tesseract`` binary (psm 7)."""
    import pytesseract  # noqa: PLC0415

    return pytesseract.image_to_string(
        img, config=f"--psm 7 -c tessedit_char_whitelist={whitelist}").strip()


def tesserocr_line(img: np.ndarray, whitelist: str) -> str:
    """Single-line read through the in-process ``tesserocr`` wheel."""
    tesserocr = _tesserocr
    if tesserocr is None:
        raise RuntimeError("tesserocr is not installed")
    from PIL import Image  # noqa: PLC0415

    api = getattr(_tls, "line_api", None)
    if api is None or getattr(_tls, "line_whitelist", None) != whitelist:
        if api is not None:
            api.End()
        api = tesserocr.PyTessBaseAPI(
            path=TESSDATA_DIR, lang="eng", psm=tesserocr.PSM.SINGLE_LINE,
            oem=tesserocr.OEM.LSTM_ONLY)
        api.SetVariable("tessedit_char_whitelist", whitelist)
        _tls.line_api, _tls.line_whitelist = api, whitelist
    api.SetImage(Image.fromarray(img))
    try:
        return (api.GetUTF8Text() or "").strip()
    except RuntimeError:
        return ""


_LINE_FOR: dict = {}  # sheet reader -> its line reader (filled below)

#: Single-line re-read variants: (scale, blur sigma as a fraction of scale).
_LINE_VARIANTS = tuple((sc, bl) for sc in (3, 4, 5, 6, 8) for bl in (0.3, 0.6))
_LINE_MIN_VOTES = 3
_LINE_MIN_SHARE = 0.4


def _line_variant(crop: np.ndarray, scale: int, blur: float) -> np.ndarray:
    r = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
    r = cv2.GaussianBlur(r, (0, 0), scale * blur)
    return cv2.copyMakeBorder(r, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)


_LINE_FOR.update({tesseract_reader: tesseract_line, tesserocr_reader: tesserocr_line})


class SheetEngine:
    """Dimension-style OCR: blob detection + batched contact-sheet voting.

    Per-crop OCR costs ~20 Tesseract launches per label (minutes on a small
    box). Here every candidate crop is a row of one tall sheet, so a variant is
    one Tesseract call for up to ``_SHEET_CELLS`` labels: a drawing needs ~10-30
    calls in total, however many labels it has.

    Each variant casts one vote per cell (Tesseract's own confidence is
    unusable under a whitelist). Round 1 uses 8 variants; only cells without a
    decisive reading go through round 2. ``conf`` is the leader's share of
    votes cast x 100. Vertical labels are read in both rotations (-90 = reads
    bottom-to-top, 90 = top-to-bottom) and near-square blobs also upright
    ("7" + ring = "7°").

    ``deadline`` (``time.monotonic`` value) stops new work once passed:
    whatever has been voted so far is used and ``budget_exceeded`` is set.
    ``cancelled`` is polled between Tesseract calls and may raise.
    """

    def __init__(self, whitelist: str = "0123456789°", *, workers: int = 2,
                 deadline: float | None = None,
                 cancelled: Callable[[], bool] | None = None,
                 reader: SheetReader | None = None,
                 line_reader: LineReader | None = None) -> None:
        self.reader = reader or tesseract_reader
        self.line_reader = line_reader or _LINE_FOR.get(self.reader)
        self.whitelist = whitelist
        self.workers = max(1, workers)
        self.deadline = deadline
        self.cancelled = cancelled
        self.budget_exceeded = False
        self._t0 = time.monotonic()

    def _out_of_time(self) -> bool:
        if self.cancelled is not None and self.cancelled():
            from backend.cancel import ClientCancelled  # noqa: PLC0415

            raise ClientCancelled("/v1/convert", "ocr")
        if self.deadline is not None and time.monotonic() > self.deadline:
            self.budget_exceeded = True
            return True
        return False

    def _vote(self, crops: list[np.ndarray]) -> tuple[list[dict[str, int]], list[int]]:
        """Run the rounds over ``crops``; return per-cell vote tables and runs."""
        votes: list[dict[str, int]] = [{} for _ in crops]
        runs = [0] * len(crops)
        live = list(range(len(crops)))
        os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # we parallelise by sheet
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for variants in _ROUNDS:
                if not live or self._out_of_time():
                    break
                tasks = [(variant, live[a:a + _SHEET_CELLS])
                         for variant in variants
                         for a in range(0, len(live), _SHEET_CELLS)]

                def work(task):
                    if self._out_of_time():
                        return None
                    (scale, shear, post), idx = task
                    sheet, bounds = _sheet([crops[i] for i in idx], scale, shear, post)
                    return idx, self.reader(sheet, bounds, self.whitelist)

                for res in pool.map(work, tasks):
                    if res is None:
                        continue
                    idx, texts = res
                    for i, txt in zip(idx, texts):
                        runs[i] += 1
                        if txt:
                            votes[i][txt] = votes[i].get(txt, 0) + 1
                live = [i for i in live
                        if votes[i] and not (
                            max(votes[i].values()) >= _DECIDE_VOTES
                            and max(votes[i].values()) >= _DECIDE_SHARE * runs[i])]
        return votes, runs

    def __call__(self, gray: np.ndarray) -> list[Word]:
        # Two line-removal variants read independently, then merged: the wide
        # one separates labels cleanly from dimension/arrow lines, the exact one
        # keeps glyph rows that touch the line. The more confident reading of
        # an overlapping pair wins.
        wide = strip_lines(gray)
        merged: list[Word] = []
        t0 = self._t0 = time.monotonic()
        for k, img in enumerate((wide, strip_lines(gray, grow=0))):
            if self._out_of_time():
                break
            started = time.monotonic()
            if k and self.deadline is not None and (
                    self.deadline - started < 1.2 * (started - t0)):
                break  # no room for a second pass: keep the first, stay in budget
            for w in self._pass(img):
                box = (w.x, w.y, w.w, w.h)
                clash = [m for m in merged if _overlap((m.x, m.y, m.w, m.h), box) > 0.3]
                if not clash:
                    merged.append(w)
                elif _replaces(w, clash):
                    merged = [m for m in merged if m not in clash] + [w]
        merged = self._refine(strip_lines(gray, grow=0), merged)
        return merged + find_repeats(wide, [w for w in merged if w.conf >= 20])

    def _refine(self, gray: np.ndarray, words: list[Word]) -> list[Word]:
        """Single-line re-read (psm 7) of horizontal boxes the sheets left unread
        or unsure about. Tesseract is markedly better on one padded line than on
        a stacked column, and plurality over scale/blur variants settles the
        digits (1390 vs "190"). Replaces a word only with a better-agreed one."""
        if self.line_reader is None:
            return words
        todo: list[tuple[int, int, int, int]] = []
        for thr in reversed(INK_THRESHOLDS):  # loosest first: widest boxes
            for x, y, w, h, orient in _text_candidates(gray, thr, stripped=True):
                if orient != "h" or h > 20 or w < 12:
                    continue
                if any(_overlap(t, (x, y, w, h)) > 0.3 for t in todo):
                    continue
                done = [m for m in words if m.conf >= 60
                        and _overlap((m.x, m.y, m.w, m.h), (x, y, w, h)) > 0.3]
                if not done:
                    todo.append((x, y, w, h))
        if not todo or self._out_of_time():
            return words
        # An optional extra: never worth the whole OCR result. Skip it when the
        # budget could not cover it, and stop quietly (no budget_exceeded flag).
        now = time.monotonic()
        if self.deadline is not None and self.deadline - now < 0.5 * (now - self._t0):
            return words

        def late() -> bool:
            return self.deadline is not None and time.monotonic() > self.deadline

        def read(box):
            if late() or (self.cancelled is not None and self.cancelled()):
                return None
            x, y, w, h = box
            crop = gray[max(y - 3, 0):y + h + 3, max(x - 6, 0):x + w + 6]
            votes: dict[str, int] = {}
            for sc, bl in _LINE_VARIANTS:
                t = self.line_reader(_line_variant(crop, sc, bl), self.whitelist)
                if t:
                    votes[t] = votes.get(t, 0) + 1
            return votes

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            results = list(pool.map(read, todo))
        out = list(words)
        for (x, y, w, h), votes in zip(todo, results):
            if not votes:
                continue
            txt, cnt = max(votes.items(), key=lambda kv: kv[1])
            share = cnt / sum(votes.values())
            if cnt < _LINE_MIN_VOTES or share < _LINE_MIN_SHARE or len(txt) < 2:
                continue
            new = Word(txt, float(x), float(y), float(w), float(h), conf=share * 100, angle=0.0)
            clash = [m for m in out if _overlap((m.x, m.y, m.w, m.h), (x, y, w, h)) > 0.3]
            if clash and not _replaces(new, clash):
                continue
            out = [m for m in out if m not in clash] + [new]
        return out

    def _pass(self, gray: np.ndarray) -> list[Word]:
        words: list[Word] = []
        boxes: list[tuple[int, int, int, int]] = []
        # Ordered groups: strict pass (h then v), then tolerant pass (h then v).
        # Acceptance (dedupe against words already read) is sequential in group
        # order; the reading itself is batched per group.
        groups = []
        for thr in INK_THRESHOLDS:
            cands = _text_candidates(gray, thr, stripped=True)
            groups += [[c for c in cands if c[4] == "h"],
                       [c for c in cands if c[4] == "v"]]
        for group in groups:
            if self._out_of_time():
                break
            todo = [c for c in group
                    if not any(_overlap(b, c[:4]) > 0.3 for b in boxes)]
            cells: list[tuple[int, float, np.ndarray]] = []
            for i, (x, y, w, h, orient) in enumerate(todo):
                crop = gray[max(y - 2, 0):y + h + 2, max(x - 3, 0):x + w + 3]
                if orient == "h":
                    cells.append((i, 0.0, crop))
                else:
                    cells.append((i, -90.0, cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE)))
                    cells.append((i, 90.0, cv2.rotate(crop, cv2.ROTATE_90_COUNTERCLOCKWISE)))
                    if h < 20:  # near-square blob: a short upright label ("7°")
                        cells.append((i, 0.0, crop))
            if not cells:
                continue
            votes, runs = self._vote([c[2] for c in cells])
            best: dict[int, tuple[str, float, float]] = {}
            ringed: set[int] = set()
            for (i, angle, crop), v, n in zip(cells, votes, runs):
                if not v or not n:
                    continue
                txt, cnt = max(v.items(), key=lambda kv: kv[1])
                if (len(txt) == 1 and txt.isdigit() and angle == 0.0
                        and crop.shape[1] <= 24 and _has_ring(crop)):
                    txt += "°"  # Tesseract rarely emits the degree sign itself
                    ringed.add(i)
                # agreement among the readings Tesseract actually produced (empty
                # reads are "no opinion"), but a lone or pair vote proves nothing
                share = cnt / sum(v.values()) if cnt >= _MIN_AGREE else cnt / n
                if i in ringed:
                    # A lone digit with a ring beside it is a degree label: the
                    # ring is evidence the vote share alone does not capture.
                    share = min(1.0, share + RING_BONUS)
                if len(txt) >= 2 and (i not in best or share > best[i][1]):
                    best[i] = (txt, share, angle)
            for i, (x, y, w, h, _) in enumerate(todo):
                if i not in best or any(_overlap(b, (x, y, w, h)) > 0.3 for b in boxes):
                    continue
                txt, share, angle = best[i]
                bw, bh = (float(w), float(h)) if angle == 0.0 else (float(h), float(w))
                if share >= WEAK_SHARE:
                    # Only a trustworthy reading claims its box; a weak one must
                    # not shadow the real label that overlaps it (a garbage
                    # horizontal blob over a vertical "350").
                    boxes.append((x, y, w, h))
                cx, cy = x + w / 2, y + h / 2
                words.append(Word(txt, cx - bw / 2, cy - bh / 2, bw, bh,
                                  conf=share * 100, angle=angle))
        return words




def blob_engine(whitelist: str = "0123456789°", **kw) -> SheetEngine:
    """Kept name for callers/tests; builds the contact-sheet engine."""
    return SheetEngine(whitelist, **kw)


def plausible(word: Word, min_chars: int) -> bool:
    """Short readings off tiny blobs are usually clutter ("13" from a door
    edge); real dimension labels have >= ``min_chars`` characters, and a
    2-character reading is only trusted when it is a degree label."""
    t = word.text.strip()
    return bool(t) and (len(t) >= min_chars or t.endswith("°"))


def detect_words(gray: np.ndarray, engine: Engine, *, min_conf: float = 60.0,
                 min_chars: int = 3) -> tuple[list[Word], list[Word]]:
    """Run OCR; return ``(accepted, rejected)`` split on confidence/plausibility."""
    words = engine(gray)
    ok = [w for w in words if w.conf >= min_conf and plausible(w, min_chars)]
    bad = [w for w in words if w not in ok]
    return ok, bad


def mask_words(gray: np.ndarray, words: list[Word], *, pad_px: int = 2,
               background: int | None = None) -> np.ndarray:
    """Copy of ``gray`` with each word box painted with the paper colour."""
    out = gray.copy()
    fill = int(np.median(gray)) if background is None else background
    for w in words:
        x0 = max(int(math.floor(w.x)) - pad_px, 0)
        y0 = max(int(math.floor(w.y)) - pad_px, 0)
        x1 = min(int(math.ceil(w.x + w.w)) + pad_px, out.shape[1])
        y1 = min(int(math.ceil(w.y + w.h)) + pad_px, out.shape[0])
        out[y0:y1, x0:x1] = fill
    return out


def word_to_polylines(word: Word) -> tuple[list[Polyline], list[str]]:
    """Single-stroke polylines for one word, in source-image px.

    Scale is fixed by the box *height* (cap height = box height, so ascender-
    less text isn't blown up) and clamped so the text never overflows the
    box width; it is then centred in the box and rotated about its centre.
    """
    lines, skipped, width = layout_text(word.text)
    if not lines or width <= 0:
        return [], skipped
    cap = svgfont.get_svg_face()["cap_height"]
    s = min(word.h / cap, word.w / width)
    cx, cy = word.x + word.w / 2, word.y + word.h / 2
    # Face space: x in [0,width], caps span y 0..cap (y-down from the cap top).
    gx, gy = width / 2, cap / 2
    th = math.radians(word.angle)
    c, sn = math.cos(th), math.sin(th)
    out: list[Polyline] = []
    for line in lines:
        pts: Polyline = []
        for px, py in line:
            dx, dy = (px - gx) * s, (py - gy) * s
            pts.append((cx + dx * c - dy * sn, cy + dx * sn + dy * c))
        out.append(pts)
    return out, skipped


def words_to_polylines(words: list[Word]) -> tuple[list[Polyline], list[str]]:
    out: list[Polyline] = []
    warnings: list[str] = []
    for w in words:
        lines, skipped = word_to_polylines(w)
        out.extend(lines)
        if skipped and WARNING_UNSUPPORTED not in warnings:
            warnings.append(WARNING_UNSUPPORTED)
    return out, warnings


def prepare(gray: np.ndarray, engine: Engine, *, min_conf: float = 60.0,
            pad_px: int = 2, min_chars: int = 3):
    """OCR + mask. Returns ``(cleaned_gray, text_polylines_px, warnings)``."""
    ok, bad = detect_words(gray, engine, min_conf=min_conf, min_chars=min_chars)
    cleaned = mask_words(gray, ok, pad_px=pad_px)
    polylines, warnings = words_to_polylines(ok)
    if bad:
        warnings.append(WARNING_LOW_CONF)
    log.debug("ocr_text words=%d rejected=%d", len(ok), len(bad))
    return cleaned, polylines, warnings
