"""Blueprint-style SVG rendering for airport ground diagrams.

Layout: pure diagram area on a fixed A4-portrait frame — all strokes,
``fill="none"``. Stays black-on-paper: the plotter draws lines, not
fills — the white-on-navy look of the reference mock is a display style,
available via the page's Display background picker.

Runway boldness is GEOMETRY, not ``stroke-width``: each strip is drawn as
a solid band of 25 parallel longitudinal paths across twice the true
width (2× ``width_ft`` schematic exaggeration — at poster scale the true
45 m strip is ~1.6 mm wide and unreadable). At typical pen/plot scales the
dense line pitch merges into a near-solid black bar, making the runway
the heaviest feature through ``parse_svg_vectors`` (which discards stroke
widths), vpype and the single-ink plot. The heavy ``stroke-width`` on the
group only affects the raw-SVG preview.

Text stays as ``<text>`` (monospace): Inkscape converts it to paths before
plotting via the iDraw extension — same step that already handles title
blocks for the other product lines.
"""

from __future__ import annotations

import hashlib
import inspect
import math
import re
import sys
from functools import lru_cache
from xml.sax.saxutils import escape as _xml_escape

from backend.airports.geometry import (
    project,
    rotate_point,
    rotation_for_heading,
    runway_heading_deg,
)
from backend.airports.ourairports import FT_TO_M, heading_from_ident, runway_endpoints
from backend.airports.overpass import (
    CONTEXT_CLASSES,
    extract_taxiway_refs,
    split_aeroway,
    split_context,
)
from backend.airports.schemas import AIRFIELD_LAYERS
from backend.http import fnum


# Version of the rendered output, baked into the SVG cache key so clients
# never see a stale layout. Derived from this file's own source: EVERY code
# change busts the cache automatically, no manual bump to forget.
def _taxiway_label_spot(
    pts: list[tuple[float, float]], size: float, text: str, segs: list[Seg], in_frame, pad: float, fits=None, hard: list[Seg] | None = None,
    apart: list[tuple[float, float]] | None = None, min_apart: float = 0.0,
    far_from: list[Seg] | None = None, min_far: float = 0.0,
) -> tuple[float, float] | None:
    """Ink centre for a taxiway designator, or None when nothing is clear: beside the route, never on it.

    Samples a few spots along the polyline and tries both sides at two
    distances; the first padded box clear of every drawn line wins.
    """
    lens = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])]
    total = sum(lens) or 1.0
    hw_u, hh_u, _, _ = _ink_metrics(text)
    hw, hh = hw_u * size, hh_u * size
    cands: list[tuple[float, float]] = []
    for frac in (0.5, 0.35, 0.65, 0.2, 0.8):
        target, acc = total * frac, 0.0
        for (a, b), ln in zip(zip(pts, pts[1:]), lens):
            if acc + ln >= target and ln > 0:
                t = (target - acc) / ln
                x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
                nx, ny = -(b[1] - a[1]) / ln, (b[0] - a[0]) / ln
                reach = hw * abs(nx) + hh * abs(ny) + pad
                for dist in (reach + size * 0.15, reach + size * 0.9):
                    for side in (1.0, -1.0):
                        c = (x + nx * dist * side, y + ny * dist * side)
                        if in_frame(*c):
                            cands.append(c)
                break
            acc += ln
    if not cands:
        return None
    return _place_label(text, size, cands, segs, pad, fits, radius=size * 3.0, hard=hard,
                        require_clear=True, tiers=_TAXI_PAD_TIERS, apart=apart, min_apart=min_apart,
                        far_from=far_from, min_far=min_far)


@lru_cache(maxsize=1)
def render_source_version() -> str:
    return hashlib.sha1(
        inspect.getsource(sys.modules[__name__]).encode("utf-8")
    ).hexdigest()[:12]

# Fixed A4-portrait frame in user units at 1000 wide; the plotter scales
# the viewBox to the page with margin (iDraw working area 210×297mm).
# Fixed aspect is what makes zoom fill the page: downstream layout does
# a contain-fit, so a content-sized frame could never fill the page on
# the other axis no matter the zoom. Here zoom=1 contains the airfield
# frame (context layers draw where they fall inside it, never reframing);
# zoom>1 covers (crops) toward a full-bleed page.
# NOTE: no title, no frequency strip, no footer, no compass in the
# artwork — the SVG is pure diagram geometry (runways, ground, badges).
# Names, frequencies and credits live on the page / API responses, never
# plotted.
_MARGIN = 40.0
_PAGE_ASPECT = 297.0 / 210.0  # A4 portrait h/w

# Runway strip treatment: schematic 2× width exaggeration + 25 longitudinal
# infill lines. True-width strips vanish at poster scale; the exaggerated
# band with a tight line pitch plots as a solid black bar. (9 lines left
# visible gaps once printed, so the pitch is ~3× finer now.)
_RUNWAY_WIDTH_EXAGGERATE = 2.0
_RUNWAY_INFILL_FRACS = tuple(-1.0 + 2.0 * i / 24 for i in range(25))

# Label clearance: a plotted label must not share ink with the geometry
# around it (the pen would draw the glyphs straight over a road/taxiway and
# the text becomes unreadable). Boxes are estimated from the monospace
# advance; padding keeps a hairline of paper between text and lines.
_LABEL_PAD_MM = 2.5  # white margin around a label box (page mm); lines are cut here
_PAGE_MM_W = 210.0  # A4 portrait width the 1000-unit canvas maps to
# Taxiway designators are decluttered harder than idents (which are always drawn):
# a ref needs this much clear paper (never relaxed) and this gap to other labels.
_TAXI_PAD_MM = 2.5
_LABEL_GAP_MM = 0.0
# No designator within this distance of a terminal outline: that is the apron-lane
# / gate zone, where OSM refs are not told apart from taxiways by tag (LHBP: G and Z
# sit at 11-13 mm, the nearest kept label at 23 mm).
_TERMINAL_CLEAR_MM = 18.0
_LABEL_SPACING_MM = 12.0  # min centre-to-centre distance between two taxiway designators
_TAXI_PAD_TIERS = (1.0,)  # no half-pad fallback for designators

Seg = tuple[tuple[float, float], tuple[float, float]]
Box = tuple[float, float, float, float]


@lru_cache(maxsize=256)
def _ink_metrics(text: str) -> tuple[float, float, float, float]:
    """True ink extents of `text` in the plotter's Hershey face, in
    font-sizes, for ``text-anchor="middle"`` at the (x, y) attributes:
    ``(half_w, half_h, centre_dx, centre_dy)`` — the ink centre sits at
    (x + centre_dx*size, y + centre_dy*size). Mirrors the layout of
    ``svg_vector._hershey_text_to_polylines`` (y is the TOP of the glyphs,
    cap height == font-size), so boxes match what the pen draws.
    """
    from backend.penplot.labels import _resolve_face

    cap, space_adv, glyphs = _resolve_face("futural", list(text))
    unit = max(cap, 1e-9)
    adv = sum(space_adv if ch == " " else (glyphs.get(ch) or {"advance": 0.0})["advance"]
              for ch in text)
    cursor = -adv / 2.0
    xs: list[float] = []
    ys: list[float] = []
    for ch in text:
        if ch == " ":
            cursor += space_adv
            continue
        g = glyphs.get(ch)
        if g is None:
            continue
        for stroke in g["lines"]:
            for gx, gy in stroke:
                xs.append((cursor + gx) / unit)
                ys.append(gy / unit)
        cursor += g["advance"]
    if not xs:
        return (0.3 * max(len(text), 1), 0.5, 0.0, 0.5)
    return ((max(xs) - min(xs)) / 2.0, (max(ys) - min(ys)) / 2.0,
            (max(xs) + min(xs)) / 2.0, (max(ys) + min(ys)) / 2.0)


def _label_box(cx: float, cy: float, size: float, text: str, pad: float = 0.0) -> Box:
    """Ink box of a label whose ink is centred on (cx, cy), grown by `pad`."""
    hw, hh, _, _ = _ink_metrics(text)
    return (cx - hw * size - pad, cy - hh * size - pad,
            cx + hw * size + pad, cy + hh * size + pad)


def _label_attrs(cx: float, cy: float, size: float, text: str) -> tuple[float, float]:
    """SVG (x, y) attributes that put the label's ink centre on (cx, cy)."""
    _, _, dx, dy = _ink_metrics(text)
    return cx - dx * size, cy - dy * size


def _pt_seg_dist(pt: tuple[float, float], seg: Seg) -> float:
    (x0, y0), (x1, y1) = seg
    dx, dy = x1 - x0, y1 - y0
    n = dx * dx + dy * dy
    t = 0.0 if n == 0 else max(0.0, min(1.0, ((pt[0] - x0) * dx + (pt[1] - y0) * dy) / n))
    return math.hypot(pt[0] - (x0 + t * dx), pt[1] - (y0 + t * dy))


def _label_order(lengths: dict[str, float]) -> list[str]:
    """Placement priority for designators: longest route first (the major
    taxiways claim clear paper before apron lanes), ties by ref — never by
    input order, so the output is deterministic."""
    return sorted(lengths, key=lambda r: (-lengths[r], r))


def _seg_hits_box(seg: Seg, box: Box) -> bool:
    """Liang-Barsky: does the segment touch the axis-aligned box?"""
    (x0, y0), (x1, y1) = seg
    xmin, ymin, xmax, ymax = box
    if max(x0, x1) < xmin or min(x0, x1) > xmax or max(y0, y1) < ymin or min(y0, y1) > ymax:
        return False
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - xmin), (dx, xmax - x0), (-dy, y0 - ymin), (dy, ymax - y0)):
        if p == 0.0:
            if q < 0.0:
                return False
            continue
        t = q / p
        if p < 0.0:
            if t > t1:
                return False
            t0 = max(t0, t)
        else:
            if t < t0:
                return False
            t1 = min(t1, t)
    return True


def _box_hits(box: Box, segs: list[Seg]) -> int:
    return sum(1 for seg in segs if _seg_hits_box(seg, box))


def _box_edges(box: Box) -> list[Seg]:
    xmin, ymin, xmax, ymax = box
    return [((xmin, ymin), (xmax, ymin)), ((xmax, ymin), (xmax, ymax)),
            ((xmax, ymax), (xmin, ymax)), ((xmin, ymax), (xmin, ymin))]


def _place_label(
    text: str, size: float, cands: list[tuple[float, float]], segs: list[Seg],
    pad: float, fits=None, keepout: Box | None = None, radius: float = 0.0,
    hard: list[Seg] | None = None, require_clear: bool = False,
    tiers: tuple[float, ...] = (1.0, 0.5),
    apart: list[tuple[float, float]] | None = None, min_apart: float = 0.0,
    far_from: list[Seg] | None = None, min_far: float = 0.0,
) -> tuple[float, float] | None:
    """Ink-centre for a label that sits in a white box of `pad` paper around it.

    Prefers a spot where the padded box crosses no drawn line (ordered
    `cands`, then a distance-sorted grid within `radius`), relaxing the pad
    to half and none; if every spot crosses something, the first candidate
    wins and the caller knocks the crossing lines out of the box — unless
    `require_clear`, which never relaxes below half the pad and returns
    ``None`` when no spot is clear. Spots must stay in the frame (`fits`)
    and out of `keepout`.
    """
    x0, y0 = cands[0]
    step = max(size * 0.5, 1.0)
    grid: list[tuple[float, float]] = []
    if radius > 0:
        n = int(radius / step)
        grid = sorted(
            ((x0 + i * step, y0 + j * step)
             for i in range(-n, n + 1) for j in range(-n, n + 1)
             if math.hypot(i, j) * step <= radius),
            key=lambda c: math.hypot(c[0] - x0, c[1] - y0),
        )
    hw_u, hh_u, _, _ = _ink_metrics(text)
    # Window covers EVERY spot we may test (far candidates included) plus the
    # label's own extent and pad: a segment outside it cannot touch any box.
    reach = max(hw_u, hh_u) * size + pad + 1.0
    xs = [c[0] for c in cands] + [x0 - radius, x0 + radius]
    ys = [c[1] for c in cands] + [y0 - radius, y0 + radius]
    window = (min(xs) - reach, min(ys) - reach, max(xs) + reach, max(ys) + reach)
    near = [sg for sg in segs if _seg_hits_box(sg, window)]
    hard_near = [sg for sg in (hard or []) if _seg_hits_box(sg, window)]
    spots = [c for c in cands + grid if _spot_ok(c, text, size, fits, keepout)
             and all(math.hypot(c[0] - ax, c[1] - ay) >= min_apart for ax, ay in (apart or []))
             and all(_pt_seg_dist(c, sg) >= min_far for sg in (far_from or []))
             and _box_hits(_label_box(c[0], c[1], size, text, pad), hard_near) == 0]
    for pad_f in ([pad * f for f in tiers] if require_clear else (pad, pad * 0.5, 0.0)):
        for cx, cy in spots:
            if _box_hits(_label_box(cx, cy, size, text, pad_f), near) == 0:
                return cx, cy
    if require_clear:
        return None
    return next(iter(spots), cands[0])


def _spot_ok(c, text, size, fits, keepout) -> bool:
    bare = _label_box(c[0], c[1], size, text)
    if keepout is not None and not (
        bare[2] < keepout[0] or keepout[2] < bare[0]
        or bare[3] < keepout[1] or keepout[3] < bare[1]
    ):
        return False
    return fits is None or fits(bare)


def _cut_polyline(pts: list[tuple[float, float]], boxes: list[Box]) -> list[list[tuple[float, float]]]:
    """Cut a polyline against label boxes: the parts inside any box vanish
    (the label's white background); the rest stay as separate polylines."""
    out: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        cuts: list[tuple[float, float]] = []
        for xmin, ymin, xmax, ymax in boxes:
            if max(x0, x1) < xmin or min(x0, x1) > xmax or max(y0, y1) < ymin or min(y0, y1) > ymax:
                continue
            t0, t1, dx, dy, ok = 0.0, 1.0, x1 - x0, y1 - y0, True
            for pp, qq in ((-dx, x0 - xmin), (dx, xmax - x0), (-dy, y0 - ymin), (dy, ymax - y0)):
                if pp == 0.0:
                    if qq < 0.0:
                        ok = False
                        break
                    continue
                t = qq / pp
                if pp < 0.0:
                    t0 = max(t0, t)
                else:
                    t1 = min(t1, t)
                if t0 > t1:
                    ok = False
                    break
            if ok:
                cuts.append((t0, t1))
        pos = 0.0
        if not cur:
            cur = [(x0, y0)]
        for t0, t1 in sorted(cuts):
            if t0 > pos:
                cur.append((x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0))
            if len(cur) > 1:
                out.append(cur)
            cur = []
            pos = max(pos, t1)
            if pos < 1.0:
                cur = [(x0 + (x1 - x0) * pos, y0 + (y1 - y0) * pos)]
        if pos < 1.0:
            cur.append((x1, y1))
        else:
            cur = []
    if len(cur) > 1:
        out.append(cur)
    return out


_PATH_RE = re.compile(r'^<path d="([^"]+)"/>$')
_NUM_RE = re.compile(r"(-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)")
# Groups never knocked out: the authoritative runway strips and their marks.
_KNOCKOUT_EXEMPT = ("runways", "runway-marks")


def _knock_out_labels(parts: list[str], boxes: list[Box]) -> list[str]:
    """Rewrite every drawn <path> so nothing runs through a label box."""
    if not boxes:
        return parts
    out: list[str] = []
    group = ""
    for part in parts:
        gm = re.match(r'<g id="([^"]+)"', part)
        if gm:
            group = gm.group(1)
        pm = _PATH_RE.match(part)
        if not pm or group in _KNOCKOUT_EXEMPT:
            out.append(part)
            continue
        d = pm.group(1)
        pts = [(float(x), float(y)) for x, y in _NUM_RE.findall(d)]
        if d.rstrip().endswith("Z"):
            pts.append(pts[0])
        pieces = _cut_polyline(pts, boxes)
        if len(pieces) == 1 and pieces[0] == pts:
            out.append(part)
            continue
        for piece in pieces:
            out.append('<path d="M ' + " L ".join(f"{x:.2f} {y:.2f}" for x, y in piece) + '"/>')
    return out


def _esc(text: object) -> str:
    return _xml_escape(str(text), {'"': "&quot;"})


def render_diagram(
    *,
    airport: dict,
    runways: list[dict],
    frequencies: list[dict],
    osm_geoms: dict[str, list[list[tuple[float, float]]]],
    context_geoms: dict[str, list[list[tuple[float, float]]]] | None = None,
    width: int = 1000,
    min_path_len_m: float = 5.0,
    zoom: float = 1.0,
    layers: list[str] | None = None,
    taxiway_refs: list[tuple[str, list[tuple[float, float]]]] | None = None,
    taxiway_labels: bool = False,
) -> tuple[str, dict[str, int], float, list[str]]:
    """Render the pure-diagram SVG (no title/strip/footer — those live on
    the page and in API responses, never plotted).

    Returns ``(svg_text, path_counts, rotation_deg, warnings)``. ``airport``
    needs ``latitude_deg/longitude_deg``. ``runways`` are raw ``runways.csv``
    rows; endpoints come from :func:`runway_endpoints` (authoritative coords
    or ident-heading fallback). ``    frequencies`` is accepted for signature
    stability but not drawn. ``taxiway_refs`` are ``(ref, way-lonlat)`` pairs
    (see :func:`extract_taxiway_refs`); a ref draws iff its way survives the
    min-detail filter and the taxiway layer is selected.
    """
    warnings: list[str] = []
    selected = set(layers) if layers is not None else set(AIRFIELD_LAYERS)
    lat0 = float(airport["latitude_deg"])
    lon0 = float(airport["longitude_deg"])

    # -- authoritative runway strips -------------------------------------
    strips: list[dict] = []
    for row in runways:
        try:
            le_ll, he_ll, derived = runway_endpoints(row, lat0, lon0)
        except Exception:
            continue
        le, he = project(le_ll[0], le_ll[1], lon0, lat0), project(he_ll[0], he_ll[1], lon0, lat0)
        length_m = (fnum(row.get("length_ft")) or 0.0) * FT_TO_M
        width_m = (fnum(row.get("width_ft")) or 0.0) * FT_TO_M
        if width_m <= 0:
            width_m = 45.0  # standard runway width fallback
            warnings.append("runway_width_fallback")
        if derived:
            warnings.append("runway_endpoints_derived")
        le_ident = (row.get("le_ident") or "").strip()
        he_ident = (row.get("he_ident") or "").strip()
        strips.append(
            {
                "le": le,
                "he": he,
                "width_m": width_m,
                "length_m": length_m,
                "le_ident": le_ident,
                "he_ident": he_ident,
                "le_deg": fnum(row.get("le_heading_degT")) or heading_from_ident(le_ident),
                "he_deg": fnum(row.get("he_heading_degT")) or heading_from_ident(he_ident),
                "le_disp_m": (fnum(row.get("le_displaced_threshold_ft")) or 0.0) * FT_TO_M,
                "he_disp_m": (fnum(row.get("he_displaced_threshold_ft")) or 0.0) * FT_TO_M,
            }
        )

    # -- single scene rotation from the primary (longest) runway ----------
    if strips:
        primary = max(strips, key=lambda s: s["length_m"])
        heading = runway_heading_deg(primary["le"], primary["he"])
        rotation = rotation_for_heading(heading)
    else:
        rotation = 0.0
        warnings.append("no_runway_data")

    strips_r = [
        {**s, "le": rotate_point(*s["le"], rotation), "he": rotate_point(*s["he"], rotation)}
        for s in strips
    ]

    def _project_polys(
        polys: list[list[tuple[float, float]]],
        closed: bool = False,
    ) -> list[list[tuple[float, float]]]:
        kept: list[list[tuple[float, float]]] = []
        for lonlat in polys:
            pts = [project(ll[0], ll[1], lon0, lat0) for ll in lonlat]
            if closed:
                # Area outlines: perimeter misleads (a 12×12 m shed has a
                # ~48 m perimeter and would survive a 30 m cutoff). Filter
                # by footprint instead — longest bbox side in meters, so
                # "min detail 30 m" hides anything smaller than 30 m across.
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                if max(max(xs) - min(xs), max(ys) - min(ys)) < min_path_len_m:
                    continue
            else:
                length = sum(
                    math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
                    for i in range(1, len(pts))
                )
                if length < min_path_len_m:
                    continue
            kept.append([rotate_point(x, y, rotation) for x, y in pts])
        return kept

    # Closed area outlines (filter by footprint extent); everything else is
    # an open centerline/polyline (filter by path length).
    _AREA_CLASSES = frozenset({
        "runway", "apron", "terminal", "hangar", "stands", "stopways",
        "water", "buildings",
    })
    osm_r: dict[str, list[list[tuple[float, float]]]] = {}
    for cls, polys in osm_geoms.items():
        osm_r[cls] = _project_polys(polys, closed=cls in _AREA_CLASSES)
    if not any(osm_r.values()):
        warnings.append("no_osm_aeroway")
    ctx_r: dict[str, list[list[tuple[float, float]]]] = {}
    if context_geoms:
        for cls, polys in context_geoms.items():
            ctx_r[cls] = _project_polys(polys, closed=cls in _AREA_CLASSES)
        if not any(ctx_r.values()):
            warnings.append("no_context_data")

    # -- fit rotated world → diagram area (AIRFIELD geometry only) ---------
    # Shop issue #10: context layers span the full fetch radius, so fitting
    # them reframes (shrinks) the airfield whenever any is enabled. The
    # frame keeps the airfield view area; selected context still draws
    # wherever it falls inside it (the frame clipping below cuts the rest).
    # With no airfield geometry at all (e.g. a context-only selection),
    # fall back to the selected-geometry fit so the view stays meaningful.
    fit_pts: list[tuple[float, float]] = []
    if "runway" in selected:
        for s in strips_r:
            fit_pts += [s["le"], s["he"]]
    for cls, polys in list(osm_r.items()):
        if cls in selected and cls in AIRFIELD_LAYERS:
            for poly in polys:
                fit_pts += poly
    if not fit_pts:
        for cls, polys in list(ctx_r.items()):
            if cls in selected:
                for poly in polys:
                    fit_pts += poly
    if not fit_pts:
        fit_pts = [(-500.0, -500.0), (500.0, 500.0)]
    min_x = min(p[0] for p in fit_pts)
    max_x = max(p[0] for p in fit_pts)
    min_y = min(p[1] for p in fit_pts)
    max_y = max(p[1] for p in fit_pts)
    pad_m = max(max_x - min_x, max_y - min_y) * 0.06 + 50.0
    min_x -= pad_m
    max_x += pad_m
    min_y -= pad_m
    max_y += pad_m

    header_h = 0.0
    total_h = width * _PAGE_ASPECT
    diagram_w = width - 2 * _MARGIN
    diagram_h = total_h - _MARGIN - header_h - _MARGIN / 2 - _MARGIN
    # Fixed A4-portrait frame (matches the default convert page aspect, so
    # downstream contain-fit never letterboxes the SVG itself). Zoom=1
    # contains all content; zoom only scales content about the center, so
    # zoom>1 covers — cropping ALL sides evenly toward a full-bleed page.
    span_x = max(max_x - min_x, 1e-9)
    span_y = max(max_y - min_y, 1e-9)
    base_scale = min(diagram_w / span_x, diagram_h / span_y)
    scale = base_scale * zoom
    # Centered mapping: zooming crops/expands about the content center so
    # the page fills evenly.
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    origin_y = _MARGIN + header_h + _MARGIN / 2

    def W2S(x: float, y: float) -> tuple[float, float]:
        # World (+x east, +y north) → SVG (+x right, +y down).
        return (_MARGIN + diagram_w / 2 + (x - cx) * scale,
                origin_y + diagram_h / 2 - (y - cy) * scale)

    # -- frame clipping (SVG space): zoomed geometry is cut at the diagram
    # border, never drawn into the margins. Open polylines use
    # Cohen–Sutherland (a line crossing the frame becomes visible runs);
    # closed outlines use Sutherland–Hodgman. Done geometrically — not via
    # clip-path, which the vector/plotter pipeline would ignore.
    _CX0, _CY0 = _MARGIN, origin_y
    _CX1, _CY1 = _MARGIN + diagram_w, origin_y + diagram_h

    def _outcode(x: float, y: float) -> int:
        code = 0
        if x < _CX0:
            code |= 1
        elif x > _CX1:
            code |= 2
        if y < _CY0:
            code |= 4
        elif y > _CY1:
            code |= 8
        return code

    def _clip_segment(
        p: tuple[float, float], q: tuple[float, float]
    ) -> tuple[tuple[float, float], tuple[float, float]] | None:
        x0, y0 = p
        x1, y1 = q
        c0, c1 = _outcode(x0, y0), _outcode(x1, y1)
        while True:
            if not (c0 | c1):
                return (x0, y0), (x1, y1)
            if c0 & c1:
                return None
            c = c0 if c0 else c1
            if c & 8:
                x = x0 + (x1 - x0) * (_CY1 - y0) / (y1 - y0) if y1 != y0 else x0
                y = _CY1
            elif c & 4:
                x = x0 + (x1 - x0) * (_CY0 - y0) / (y1 - y0) if y1 != y0 else x0
                y = _CY0
            elif c & 2:
                y = y0 + (y1 - y0) * (_CX1 - x0) / (x1 - x0) if x1 != x0 else y0
                x = _CX1
            else:
                y = y0 + (y1 - y0) * (_CX0 - x0) / (x1 - x0) if x1 != x0 else y0
                x = _CX0
            if c == c0:
                x0, y0, c0 = x, y, _outcode(x, y)
            else:
                x1, y1, c1 = x, y, _outcode(x, y)

    def _clip_open(pts: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
        runs: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        for i in range(len(pts) - 1):
            seg = _clip_segment(pts[i], pts[i + 1])
            if seg is None:
                if len(current) >= 2:
                    runs.append(current)
                current = []
                continue
            a, b = seg
            if current and current[-1] == a:
                current.append(b)
            else:
                if len(current) >= 2:
                    runs.append(current)
                current = [a, b]
        if len(current) >= 2:
            runs.append(current)
        return runs

    def _clip_edge(
        pts: list[tuple[float, float]], inside, intersect
    ) -> list[tuple[float, float]]:
        if not pts:
            return []
        out = [pts[0]] if inside(pts[0]) else []
        for i in range(1, len(pts)):
            prev, cur = pts[i - 1], pts[i]
            cur_in, prev_in = inside(cur), inside(prev)
            if cur_in:
                if not prev_in:
                    out.append(intersect(prev, cur))
                out.append(cur)
            elif prev_in:
                out.append(intersect(prev, cur))
        return out

    def _clip_closed(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
        ring = list(pts)
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        for inside, intersect in (
            (lambda p: p[0] >= _CX0,
             lambda a, b: (_CX0, a[1] + (b[1] - a[1]) * (_CX0 - a[0]) / (b[0] - a[0]))),
            (lambda p: p[0] <= _CX1,
             lambda a, b: (_CX1, a[1] + (b[1] - a[1]) * (_CX1 - a[0]) / (b[0] - a[0]))),
            (lambda p: p[1] >= _CY0,
             lambda a, b: (a[0] + (b[0] - a[0]) * (_CY0 - a[1]) / (b[1] - a[1]), _CY0)),
            (lambda p: p[1] <= _CY1,
             lambda a, b: (a[0] + (b[0] - a[0]) * (_CY1 - a[1]) / (b[1] - a[1]), _CY1)),
        ):
            ring = _clip_edge(ring, inside, intersect)
            if len(ring) < 3:
                return []
        return ring

    def _in_frame(x: float, y: float) -> bool:
        return _CX0 <= x <= _CX1 and _CY0 <= y <= _CY1

    def _box_in_frame(box: Box) -> bool:
        return _CX0 <= box[0] and box[2] <= _CX1 and _CY0 <= box[1] and box[3] <= _CY1

    # -- stroke weights (preview only; hierarchy survives via geometry) ----
    runway_w = max(6.0, width / 130.0)
    taxi_w = max(1.2, width / 700.0)
    thin_w = max(0.8, width / 1100.0)
    text_h = max(11.0, width / 72.0)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{total_h:.1f}" viewBox="0 0 {width} {total_h:.1f}">',
        f"<!-- {_esc(airport.get('name', ''))} "
        f"({_esc(airport.get('ident', ''))}) — rotation {rotation:.1f}deg CCW; "
        "runway edges are geometry (plotter-safe), not stroke-width -->",
    ]
    counts = {"runway": 0, "taxiway": 0, "apron": 0, "terminal": 0,
              "hangar": 0, "stands": 0, "stopways": 0,
              **{cls: 0 for cls in CONTEXT_CLASSES}}

    # NOTE: no in-SVG title — name/country live as fixed labels at the top
    # of the /airports page (and the convert title block), never plotted.
    # NOTE: no frequency strip either — frequencies travel in the API
    # responses and page copy; the artwork is pure diagram geometry.

    # Every drawn line segment (SVG space): labels are placed clear of them.
    obstacles: list[Seg] = []
    runway_hard: list[Seg] = []  # strip infill lines: labels may never touch these
    label_boxes: list[Box] = []  # padded label boxes: lines are cut out of them
    label_pad = width * _LABEL_PAD_MM / _PAGE_MM_W
    taxi_pad = width * _TAXI_PAD_MM / _PAGE_MM_W
    label_gap = width * _LABEL_GAP_MM / _PAGE_MM_W
    label_spacing = width * _LABEL_SPACING_MM / _PAGE_MM_W
    placed_centres: list[tuple[float, float]] = []
    terminal_clear = width * _TERMINAL_CLEAR_MM / _PAGE_MM_W
    terminal_segs: list[Seg] = []
    for ring in osm_r.get("terminal", []):
        tp = [W2S(*q) for q in ring]
        terminal_segs += list(zip(tp, tp[1:])) + ([(tp[-1], tp[0])] if len(tp) > 2 else [])

    # -- surrounding context first (faintest, under the airfield) ---------
    def clipped_d(world_poly: list[tuple[float, float]], close: bool,
                  sink: list[Seg] | None = None) -> list[str]:
        """Map world → SVG, cut at the diagram frame, return path strings."""
        pts = [W2S(x, y) for x, y in world_poly]
        if close:
            ring = _clip_closed(pts)
            seqs = [ring] if len(ring) >= 3 else []
        else:
            seqs = _clip_open(pts)
        out = []
        for seq in seqs:
            obstacles.extend(zip(seq, seq[1:]))
            if sink is not None:
                sink.extend(zip(seq, seq[1:]))
            if close:
                obstacles.append((seq[-1], seq[0]))
            d = f"M {seq[0][0]:.2f} {seq[0][1]:.2f} " + " ".join(
                f"L {x:.2f} {y:.2f}" for x, y in seq[1:]
            )
            if close:
                d += " Z"
            out.append(d)
        return out

    faint_w = max(0.5, width / 1600.0)
    for cls in CONTEXT_CLASSES:
        if cls not in selected:
            continue
        parts.append(
            f'<g id="context-{cls}" fill="none" stroke="#000000" '
            f'stroke-width="{faint_w:.2f}" stroke-linecap="round" stroke-linejoin="round">'
        )
        for poly in ctx_r.get(cls, []):
            for d in clipped_d(poly, cls not in ("highways", "roads", "paths", "rails")):
                parts.append(f"<path d=\"{d}\"/>")
                counts[cls] += 1
        parts.append("</g>")

    # -- OSM ground: taxiway centerlines; apron/terminal/hangar outlines ---
    for cls, gid, w, closed in (
        ("taxiway", "taxiways", taxi_w, False),
        ("apron", "aprons", thin_w, True),
        ("terminal", "terminals", thin_w, True),
        ("hangar", "hangars", thin_w, True),
        ("stands", "stands", thin_w, True),
        ("stopways", "stopways", thin_w, True),
    ):
        if cls not in selected:
            continue
        parts.append(
            f'<g id="osm-{gid}" fill="none" stroke="#000000" '
            f'stroke-width="{w:.2f}" stroke-linecap="round" stroke-linejoin="round">'
        )
        for poly in osm_r.get(cls, []):
            for d in clipped_d(poly, closed):
                parts.append(f"<path d=\"{d}\"/>")
                counts[cls] += 1
        parts.append("</g>")

    show_runway = "runway" in selected
    # -- OSM runway outlines (thin, under the authoritative strip) ----------
    if show_runway:
        parts.append(
            f'<g id="osm-runways" fill="none" stroke="#000000" '
            f'stroke-width="{thin_w:.2f}" stroke-linecap="round" stroke-linejoin="round">'
        )
        for poly in osm_r.get("runway", []):
            for d in clipped_d(poly, True):
                parts.append(f"<path d=\"{d}\"/>")
                counts["runway"] += 1
        parts.append("</g>")

    # -- authoritative strips: solid band (geometry-bold) ------------------
    # 25 longitudinal paths across 2× the true width. The exaggeration is
    # deliberate: at poster scale a true 45 m strip is ~1.6 mm wide and the
    # infill pitch merges into a near-solid black bar — the
    # heaviest feature on the page. Pure geometry: stroke-width would not
    # survive the vector pipeline.
    if show_runway:
        parts.append(
            f'<g id="runways" fill="none" stroke="#000000" '
            f'stroke-width="{runway_w:.2f}" stroke-linecap="butt">'
        )
        for s in strips_r:
            dx, dy = s["he"][0] - s["le"][0], s["he"][1] - s["le"][1]
            seg = math.hypot(dx, dy) or 1.0
            nx, ny = -dy / seg, dx / seg  # world-space normal
            hw = s["width_m"] / 2.0 * _RUNWAY_WIDTH_EXAGGERATE
            strips_world = [
                [(s["le"][0] + nx * hw * frac, s["le"][1] + ny * hw * frac),
                 (s["he"][0] + nx * hw * frac, s["he"][1] + ny * hw * frac)]
                for frac in _RUNWAY_INFILL_FRACS
            ]
            for world_seg in strips_world:
                for d in clipped_d(world_seg, False, runway_hard):
                    parts.append(f"<path d=\"{d}\"/>")
                    counts["runway"] += 1
        parts.append("</g>")

    # -- taxiway designators (opt-in annotations of the taxiway layer) -----
    # One label per designator (OSM splits ways arbitrarily): keep the
    # longest surviving way so the ref sits on the major stretch. A ref
    # draws iff its way survives the min-detail filter, the taxiway layer
    # is selected, and the midpoint lands in frame. Declutter: longer
    # routes place first, and a designator that finds no clear paper (its
    # padded box would cross a line or another label — apron lanes with
    # gate-like refs such as P1/H2 live in exactly that clutter) is dropped,
    # never forced. Runway idents are the opposite: always drawn, lines cut.
    if taxiway_labels and "taxiway" in selected and taxiway_refs:
        best: dict[str, tuple[float, list[tuple[float, float]]]] = {}
        for ref, lonlat in taxiway_refs:
            kept = _project_polys([lonlat], closed=False)
            if not kept:
                continue
            poly = kept[0]
            length = sum(
                math.hypot(poly[i][0] - poly[i - 1][0], poly[i][1] - poly[i - 1][1])
                for i in range(1, len(poly))
            )
            if ref not in best or length > best[ref][0]:
                best[ref] = (length, poly)
        lines: list[str] = []
        for ref in _label_order({r: ln for r, (ln, _) in best.items()}):
            pts = [W2S(*p) for p in best[ref][1]]
            mx, my = pts[len(pts) // 2]
            if not _in_frame(mx, my):
                continue
            spot = _taxiway_label_spot(
                pts, text_h, ref, obstacles, _in_frame, taxi_pad, _box_in_frame,
                runway_hard, placed_centres, label_spacing, terminal_segs, terminal_clear)
            if spot is None:
                continue  # no clear paper: decluttered
            lx, ly = spot
            lsz = text_h
            label_boxes.append(_label_box(lx, ly, lsz, ref, label_pad))
            placed_centres.append((lx, ly))
            obstacles.extend(_box_edges(_label_box(lx, ly, lsz, ref, label_gap)))
            tx, ty = _label_attrs(lx, ly, lsz, ref)
            lines.append(
                f'<text x="{tx:.2f}" y="{ty:.2f}" text-anchor="middle" '
                f'font-family="monospace" data-stroke-font="hershey" font-size="{lsz:.1f}" '
                f'stroke="none" fill="#000000">{_esc(ref)}</text>'
            )
        if lines:
            parts.append('<g id="taxiway-labels" fill="none">')
            parts.extend(lines)
            parts.append("</g>")

    # -- badges (ident) + displaced-threshold ticks -------------------------
    # Annotations of the runway layer: hidden with it. (No degree ovals —
    # the ident already encodes the heading; the oval was chart clutter.)
    if show_runway:
        parts.append(
            f'<g id="runway-marks" fill="none" stroke="#000000" '
            f'stroke-width="{thin_w:.2f}">'
        )
    for s in (strips_r if show_runway else []):
        x1, y1 = W2S(*s["le"])
        x2, y2 = W2S(*s["he"])
        dx, dy = x2 - x1, y2 - y1
        seg = math.hypot(dx, dy) or 1.0
        ux, uy = dx / seg, dy / seg
        nx, ny = -uy, ux
        half_band = (s["width_m"] * _RUNWAY_WIDTH_EXAGGERATE * scale) / 2.0
        for (px, py), ident, disp_m, end_sign in (
            ((x1, y1), s["le_ident"], s["le_disp_m"], -1.0),
            ((x2, y2), s["he_ident"], s["he_disp_m"], +1.0),
        ):
            # Off-frame runway ends (zoomed in) lose their furniture too.
            if not _in_frame(px, py):
                continue
            # Displaced-threshold tick: perpendicular bar disp_m inside the end.
            if disp_m > 0 and s["length_m"] > 0:
                frac = disp_m / s["length_m"]
                tx = px - ux * end_sign * frac * seg
                ty = py - uy * end_sign * frac * seg
                tick = _clip_segment(
                    (tx - nx * half_band, ty - ny * half_band),
                    (tx + nx * half_band, ty + ny * half_band),
                )
                if tick is not None:
                    (ax_, ay_), (bx_, by_) = tick
                    parts.append(
                        f"<path d=\"M {ax_:.2f} {ay_:.2f} L {bx_:.2f} {by_:.2f}\"/>"
                    )
            # Ident label past the threshold, with a gap to the strip end and
            # pushed further out / sideways until it is clear of roads.
            isz = text_h * 1.4
            hw_u, hh_u, _, _ = _ink_metrics(ident)
            reach = hw_u * isz * abs(ux) + hh_u * isz * abs(uy)  # half-extent along the axis
            # Never closer than before (band half-width + 1.2 glyphs), and at
            # least one glyph of clear paper between strip end and label.
            base = max(half_band + text_h * 1.2, reach + label_pad + text_h * 0.3)
            cands = [
                (px + ux * end_sign * (base + k * text_h * 0.6) + nx * lat * text_h,
                 py + uy * end_sign * (base + k * text_h * 0.6) + ny * lat * text_h)
                for k in range(10) for lat in (0.0, 1.2, -1.2, 2.4, -2.4)
            ]
            # Keep-out: the strip end plus one glyph of paper beyond it.
            ex0, ey0 = px - nx * half_band, py - ny * half_band
            ex1, ey1 = (px + ux * end_sign * text_h * 1.0 + nx * half_band,
                        py + uy * end_sign * (label_pad + text_h * 0.3) + ny * half_band)
            keep = (min(ex0, ex1, px - nx * half_band, px + nx * half_band) - 1.0,
                    min(ey0, ey1, py - ny * half_band, py + ny * half_band) - 1.0,
                    max(ex0, ex1, px - nx * half_band, px + nx * half_band) + 1.0,
                    max(ey0, ey1, py - ny * half_band, py + ny * half_band) + 1.0)
            ix, iy = _place_label(
                ident, isz, cands, obstacles, label_pad, _box_in_frame,
                keepout=keep, radius=text_h * 5.0, hard=runway_hard)
            isz_used = isz
            label_boxes.append(_label_box(ix, iy, isz, ident, label_pad))
            obstacles.extend(_box_edges(_label_box(ix, iy, isz_used, ident)))
            tx, ty = _label_attrs(ix, iy, isz_used, ident)
            parts.append(
                f'<text x="{tx:.2f}" y="{ty:.2f}" text-anchor="middle" '
                f'font-family="monospace" data-stroke-font="hershey" font-size="{isz_used:.1f}" '
                f'stroke="none" fill="#000000">{_esc(ident)}</text>'
            )
    if show_runway:
        parts.append("</g>")

    # NOTE: no compass — client decision, removed from the artwork.

    parts.append("</svg>")
    parts = _knock_out_labels(parts, label_boxes)
    return "\n".join(parts) + "\n", counts, rotation, warnings


def _build_svg(
    airport: dict,
    runway_rows: list[dict],
    freq_rows: list[dict],
    elements: list[dict],
    width: int,
    min_path_len_m: float,
    context_elements: list[dict] | None = None,
    zoom: float = 1.0,
    layers: list[str] | None = None,
    taxiway_labels: bool = False,
    cancelled: object = None,
    geoms: dict[str, list[list[tuple[float, float]]]] | None = None,
    raw_counts: dict[str, int] | None = None,
    ctx_geoms: dict[str, list[list[tuple[float, float]]]] | None = None,
) -> tuple[str, dict[str, int], dict[str, int], float, list[str],
           tuple[dict, dict] | None, dict | None]:
    """Split polygons and render the blueprint SVG (shared by the airport
    render/import endpoints via ``_render_uncached``; moved verbatim from
    the router in REF-002 Phase 2 — pure composition, no HTTP in here).

    Reuses cached split outputs when given; otherwise splits here and
    returns what was computed so the caller can persist it next to the
    render cache.
    """
    computed_aer: tuple[dict, dict] | None = None
    if geoms is None:
        geoms, raw_counts = split_aeroway(
            elements, cancelled=cancelled if callable(cancelled) else None)
        computed_aer = (geoms, raw_counts)
    assert raw_counts is not None
    twy_refs = extract_taxiway_refs(elements) if taxiway_labels else None
    computed_ctx: dict | None = None
    if context_elements:
        if ctx_geoms is None:
            ctx_geoms = split_context(context_elements)
            computed_ctx = ctx_geoms
        raw_counts = {
            **raw_counts,
            "context_ways": sum(len(v) for v in ctx_geoms.values()),
        }
    else:
        ctx_geoms = {}
    svg, path_counts, rotation, warnings = render_diagram(
        airport=airport,
        runways=runway_rows,
        frequencies=[
            {
                "type": r["type"],
                "description": r["description"],
                "frequency_mhz": r["frequency_mhz"],
            }
            for r in freq_rows
        ],
        osm_geoms=geoms,
        context_geoms=ctx_geoms,
        width=width,
        min_path_len_m=min_path_len_m,
        zoom=zoom,
        layers=layers,
        taxiway_refs=twy_refs,
        taxiway_labels=taxiway_labels,
    )
    return svg, path_counts, raw_counts, rotation, warnings, computed_aer, computed_ctx


__all__ = ["render_diagram"]
