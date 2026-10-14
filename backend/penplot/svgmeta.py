"""Ownership / licence metadata for generated SVGs.

SVG has standard homes for it: ``<title>``, ``<desc>`` and ``<metadata>`` with
RDF (Dublin Core ``dc:`` + Creative Commons ``cc:``, the convention Inkscape
uses) plus Adobe's ``xmp:CreatorTool`` for the generator. ``stamp`` inserts
them right after the root ``<svg>`` tag; plotters, vpype and browsers ignore
them, so the geometry is untouched.

It is a *notice*, not protection: editors and optimisers (svgo) can strip it.
It records who made the file and on what terms. Deliberately:

* **server-enforced** - creator / rights / licence come from ``Settings``
  (``PENPLOT_SVG_*``); clients may only add a ``title`` and ``description``,
  so a request can neither remove nor replace the owner's notice;
* **off unless configured** - with nothing set ``stamp`` returns its input
  unchanged, so existing output (and its byte-for-byte determinism) is intact;
* **deterministic** - no timestamps, so the same input still yields the same
  bytes; put the year in ``svg_rights`` ("(c) 2026 ...");
* **safe** - every value is XML-escaped and stripped of control characters;
  licence URLs must be http(s).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from xml.sax.saxutils import escape

from backend.penplot.config import Settings

GENERATOR = "fund-vista penplot"

#: Marker id for the download-only visible credit line (serve-time, never
#: stored). The SVG parser skips this subtree on re-ingest, so a downloaded
#: file converts back to the same geometry. The shop-side guard
#: (``downloadDesignSvg``) passes stamped bytes through unchanged.
CREDIT_GROUP_ID = "penplot-credit"
#: Shop-side downloads already carry this host line; treat it as credited so
#: a re-served file never grows a second line (idempotent across backends).
CREDIT_KNOWN_HOST = "penplot.linuxadm.hu"

CC_NS = "http://creativecommons.org/ns#"
_NAMESPACES = (
    ("xmlns:rdf", "http://www.w3.org/1999/02/22-rdf-syntax-ns#"),
    ("xmlns:dc", "http://purl.org/dc/elements/1.1/"),
    ("xmlns:cc", CC_NS),
    ("xmlns:xmp", "http://ns.adobe.com/xap/1.0/"),
)

# preset -> (display name, deed URL or None, permits, requires, prohibits)
LICENSES: dict[str, tuple[str, str | None, tuple, tuple, tuple]] = {
    "all-rights-reserved": ("All rights reserved", None, (), (), ()),
    "cc-by-4.0": ("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/",
                  ("Reproduction", "Distribution", "DerivativeWorks"),
                  ("Notice", "Attribution"), ()),
    "cc-by-sa-4.0": ("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/",
                     ("Reproduction", "Distribution", "DerivativeWorks"),
                     ("Notice", "Attribution", "ShareAlike"), ()),
    "cc-by-nc-4.0": ("CC BY-NC 4.0", "https://creativecommons.org/licenses/by-nc/4.0/",
                     ("Reproduction", "Distribution", "DerivativeWorks"),
                     ("Notice", "Attribution"), ("CommercialUse",)),
    "cc-by-nc-sa-4.0": ("CC BY-NC-SA 4.0",
                        "https://creativecommons.org/licenses/by-nc-sa/4.0/",
                        ("Reproduction", "Distribution", "DerivativeWorks"),
                        ("Notice", "Attribution", "ShareAlike"), ("CommercialUse",)),
    "cc-by-nc-nd-4.0": ("CC BY-NC-ND 4.0",
                        "https://creativecommons.org/licenses/by-nc-nd/4.0/",
                        ("Reproduction", "Distribution"),
                        ("Notice", "Attribution"), ("CommercialUse",)),
}

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ROOT = re.compile(r"<svg\b[^>]*?(?<!/)>", re.DOTALL)
_VIEWBOX = re.compile(r"""viewBox\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_WIDTH_ATTR = re.compile(r"""<svg\b[^>]*?\bwidth\s*=\s*["']([^"']+)["']""", re.IGNORECASE | re.DOTALL)
_HEIGHT_ATTR = re.compile(r"""<svg\b[^>]*?\bheight\s*=\s*["']([^"']+)["']""", re.IGNORECASE | re.DOTALL)
_CREDIT_ID = re.compile(r"""id\s*=\s*["'][^"']*credit[^"']*["']""", re.IGNORECASE)
_CLOSE_SVG = re.compile(r"</svg\s*>", re.IGNORECASE)
_NUM = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


def clean(text: str | None, limit: int = 500) -> str:
    """Plain single-line text: no control chars, collapsed whitespace, capped."""
    return re.sub(r"\s+", " ", _CTRL.sub("", text or "")).strip()[:limit]


def validate_url(url: str) -> str:
    url = (url or "").strip()
    if url and not re.match(r"^https?://[^\s<>\"']+$", url):
        raise ValueError("must be an http(s) URL")
    return url


def validate_license(name: str) -> str:
    name = (name or "").strip().lower()
    if name and name not in LICENSES:
        raise ValueError(f"unknown licence {name!r}; one of {', '.join(sorted(LICENSES))}")
    return name


@dataclass(frozen=True)
class Meta:
    title: str = ""
    description: str = ""
    creator: str = ""
    rights: str = ""
    license: str = ""         # preset key or ""
    license_url: str = ""
    attribution_url: str = ""

    @property
    def active(self) -> bool:
        return any((self.title, self.description, self.creator, self.rights,
                    self.license, self.license_url, self.attribution_url))


def resolve(settings: Settings | None = None, *, title: str = "",
            description: str = "") -> Meta:
    """Server-configured ownership fields + the per-request title/description."""
    s = settings or Settings()
    return Meta(
        title=clean(title, 200), description=clean(description, 500),
        creator=clean(s.svg_creator, 200), rights=clean(s.svg_rights, 500),
        license=s.svg_license, license_url=s.svg_license_url,
        attribution_url=s.svg_attribution_url)


def salt(settings: Settings | None = None) -> str:
    """Short digest of the server-side ownership config ("" when unset).

    Mixed into result filenames so changing the config can never serve a cached
    SVG stamped with the old (or no) notice.
    """
    m = resolve(settings)
    parts = (m.creator, m.rights, m.license, m.license_url, m.attribution_url)
    if not any(parts):
        return ""
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:8]


def _attr(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def _agent(tag: str, text: str) -> str:
    return f"<{tag}><cc:Agent><dc:title>{escape(text)}</dc:title></cc:Agent></{tag}>"


def build_block(m: Meta) -> str:
    """``<title>``, ``<desc>`` and ``<metadata>`` for ``m`` (no root tag)."""
    name, deed, permits, requires, prohibits = LICENSES.get(m.license, ("", None, (), (), ()))
    deed = m.license_url or deed
    rights = m.rights
    if not rights and m.creator:
        rights = f"© {m.creator}"
    if m.license and name and name not in rights:
        sep = " " if rights.endswith((".", "!", "?")) else ". "
        rights = f"{rights}{sep}Licence: {name}." if rights else f"Licence: {name}."
    desc_parts = [p for p in (m.description, rights, f"Generated by {GENERATOR}.") if p]

    out = []
    if m.title:
        out.append(f"<title>{escape(m.title)}</title>")
    out.append(f"<desc>{escape(' '.join(desc_parts))}</desc>")
    work = ['<cc:Work rdf:about="">', "<dc:format>image/svg+xml</dc:format>",
            '<dc:type rdf:resource="http://purl.org/dc/dcmitype/StillImage"/>']
    if m.title:
        work.append(f"<dc:title>{escape(m.title)}</dc:title>")
    if m.creator:
        work.append(_agent("dc:creator", m.creator))
    if rights:
        work.append(_agent("dc:rights", rights))
    if m.attribution_url:
        work.append(f'<cc:attributionURL rdf:resource="{_attr(m.attribution_url)}"/>')
    if m.creator and (m.license or m.license_url):
        work.append(f"<cc:attributionName>{escape(m.creator)}</cc:attributionName>")
    if deed:
        work.append(f'<cc:license rdf:resource="{_attr(deed)}"/>')
    work.append("</cc:Work>")
    rdf = ["<rdf:RDF>", *work]
    if deed and (permits or requires or prohibits):
        rdf.append(f'<cc:License rdf:about="{_attr(deed)}">')
        rdf += [f'<cc:permits rdf:resource="{CC_NS}{p}"/>' for p in permits]
        rdf += [f'<cc:requires rdf:resource="{CC_NS}{r}"/>' for r in requires]
        rdf += [f'<cc:prohibits rdf:resource="{CC_NS}{x}"/>' for x in prohibits]
        rdf.append("</cc:License>")
    rdf.append(f'<rdf:Description rdf:about="" xmp:CreatorTool="{_attr(GENERATOR)}"/>')
    rdf.append("</rdf:RDF>")
    out.append("<metadata>" + "".join(rdf) + "</metadata>")
    return "".join(out)


def stamp(svg_text: str, settings: Settings | None = None, *, title: str = "",
          description: str = "", require_owner: bool = False) -> str:
    """Insert the metadata block after the root ``<svg>`` tag.

    No-op (returns ``svg_text`` unchanged) when nothing is configured or given,
    when the text has no ordinary root tag, or when it was already stamped.
    ``require_owner`` additionally skips it unless the server has an ownership
    config (creator / rights / licence): a bare title then changes nothing.
    """
    meta = resolve(settings, title=title, description=description)
    if not meta.active or "<metadata" in svg_text:
        return svg_text
    if require_owner and not salt(settings):
        return svg_text
    m = _ROOT.search(svg_text)
    if m is None:
        return svg_text
    tag = m.group(0)
    extra = "".join(f' {k}="{v}"' for k, v in _NAMESPACES if f"{k}=" not in tag)
    new_tag = tag[:-1] + extra + ">"
    return svg_text[:m.start()] + new_tag + build_block(meta) + svg_text[m.end():]


def credit_domain(settings: Settings | None = None) -> str:
    """Visible-credit host derived from the configured attribution URL.

    Returns the lowercased hostname (``""`` when unconfigured or unparsable),
    so the download line reuses the ``svgmeta`` vocabulary instead of a new
    setting. ``https://penplot.linuxadm.hu/shop`` -> ``penplot.linuxadm.hu``.
    """
    raw = (resolve(settings).attribution_url or "").strip()
    if not raw:
        return ""
    from urllib.parse import urlparse

    try:
        host = (urlparse(raw).hostname or "").strip().lower()
    except ValueError:
        return ""
    return host


def has_visible_credit(svg_text: str) -> bool:
    """True when a download credit line is already present (any backend).

    Detects our own marker (``penplot-credit`` / ``data-credit``) and any
    ``id="...credit..."`` (covers the shop-side guard's id). The known shop
    host line only counts outside ``<metadata>`` — the metadata block itself
    carries the attribution URL, which must not suppress the visible line.
    """
    if CREDIT_GROUP_ID in svg_text or "data-credit" in svg_text.lower():
        return True
    if _CREDIT_ID.search(svg_text):
        return True
    body = re.sub(r"<metadata\b.*?</metadata\s*>", "", svg_text,
                  flags=re.DOTALL | re.IGNORECASE)
    return CREDIT_KNOWN_HOST in body


def _page_for_credit(svg_text: str) -> tuple[float, float, float, float] | None:
    """ViewBox-aware page rect ``(minx, miny, w, h)`` for credit placement.

    Prefers the root ``viewBox`` (the coordinate space the credit ``<text>``
    lives in); falls back to root ``width``/``height``. ``None`` when neither
    parses, in which case the caller leaves the bytes untouched.
    """
    m = _VIEWBOX.search(svg_text)
    if m:
        nums = _NUM.findall(m.group(1))
        try:
            minx, miny, w, h = (float(n) for n in nums[:4])
        except (ValueError, IndexError):
            pass
        else:
            if w > 0 and h > 0:
                return minx, miny, w, h
    wm = _WIDTH_ATTR.search(svg_text)
    hm = _HEIGHT_ATTR.search(svg_text)
    if wm and hm:
        try:
            w = float(_NUM.findall(wm.group(1))[0])
            h = float(_NUM.findall(hm.group(1))[0])
        except (ValueError, IndexError):
            return None
        if w > 0 and h > 0:
            return 0.0, 0.0, w, h
    return None


def stamp_visible_credit(svg_text: str, settings: Settings | None = None) -> str:
    """Append the download-only visible domain line; previews stay clean.

    Centred ``<text>`` in the bottom out-of-border margin, placed
    viewBox-aware (``x = minx + w/2``, ``y`` ~one font-size above the page
    bottom). Plot geometry is untouched: only this element plus metadata
    (via :func:`stamp_download`) are ever added, and the SVG parser skips
    the credit subtree on re-ingest.

    No-op when no attribution host is configured, when placement does not
    parse, or when a credit line is already present. Deterministic: no
    timestamps, same input yields the same bytes.
    """
    domain = credit_domain(settings)
    if not domain or has_visible_credit(svg_text):
        return svg_text
    # Same domain already visible as plain text (e.g. a foreign stamper kept
    # the text but not our marker): strip metadata first so the attribution
    # URL itself never counts.
    body = re.sub(r"<metadata\b.*?</metadata\s*>", "", svg_text,
                  flags=re.DOTALL | re.IGNORECASE)
    if domain in body:
        return svg_text
    page = _page_for_credit(svg_text)
    if page is None:
        return svg_text
    minx, miny, w, h = page
    closings = list(_CLOSE_SVG.finditer(svg_text))
    if not closings:
        return svg_text
    font_size = min(4.0, max(2.5, h * 0.012))
    x = minx + w / 2.0
    y = miny + h - font_size * 0.85
    line = (
        f'<g id="{CREDIT_GROUP_ID}" data-credit="1">'
        f'<text x="{x:.3f}" y="{y:.3f}" text-anchor="middle" '
        f'font-family="sans-serif" font-size="{font_size:.2f}" '
        f'fill="#888888" stroke="none">{escape(domain)}</text></g>'
    )
    end = closings[-1].start()
    return svg_text[:end] + line + svg_text[end:]


def stamp_download(svg_text: str, settings: Settings | None = None, *, title: str = "",
                   description: str = "") -> str:
    """Serve-time download variant: metadata notice + visible domain line.

    Never stored: the results endpoint applies this to cached bytes on
    ``GET /v1/results/{file}?download=1`` (or ``?credit=1``) so previews and
    cached files stay clean and deterministic. Idempotent: re-requesting or
    feeding an already-stamped file (either backend) is a byte-identical
    no-op; geometry (``<path>`` data) is never modified.
    """
    return stamp_visible_credit(stamp(svg_text, settings, title=title,
                                      description=description), settings)
